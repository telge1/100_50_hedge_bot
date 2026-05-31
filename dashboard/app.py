#!/usr/bin/env python3
"""
Dashboard Web Application
"""
from fastapi import FastAPI, Request, Form, HTTPException, Depends, Body, Query, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import subprocess
import uvicorn
import os
import time
from functools import lru_cache
import requests
import asyncio
import httpx
import uuid
import json
import math
import signal
from typing import Set, Dict, List, Optional, Any, Iterable, Tuple
import threading
import re
import logging
import sys
import yaml
from datetime import datetime, timezone, timedelta
import shutil
import ast
from pathlib import Path
import subprocess

# Add parent directory to path for imports
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))
vendor_root = Path(__file__).resolve().parent / "vendor"
if vendor_root.exists():
    sys.path.insert(0, str(vendor_root))
os.environ.setdefault("BURN_REENTRY_PROJECT_ROOT", str(project_root))

CONFIRMED_ORDER_PNL_HISTORY_FILE = project_root / "logs" / "confirmed_order_pnl_history.jsonl"
DASHBOARD_CLOSED_PNL_HISTORY_FILE = project_root / "logs" / "dashboard_closed_pnl_history.jsonl"
GLOBAL_RUNTIME_LOG_PATH = project_root / "logs" / "fixed_cycle_hedge_runtime.log"

LIVE_BOT_LOGS_ROOT = project_root / "live_bots" / "100_50_hedge_bot"
WATCHER_SCRIPT_ROOT = LIVE_BOT_LOGS_ROOT / "shared_scripts"
START_WATCHERS_SCRIPT = WATCHER_SCRIPT_ROOT / "start_hedge_guard_watchers.sh"
STOP_WATCHERS_SCRIPT = WATCHER_SCRIPT_ROOT / "stop_hedge_guard_watchers.sh"
WATCHERS_STATUS_FILE = LIVE_BOT_LOGS_ROOT / "run" / "hedge_guard_watchers_status.json"
ACCOUNT_PNL_PATHS: dict[str, dict[str, Path]] = {}
WALLET_SNAPSHOT_FILES: dict[str, Path] = {}
BOT_STATE_FILES: dict[str, Path] = {}

WALLET_SNAPSHOT_TTL_SECONDS = 60
WALLET_SNAPSHOT_SCRIPT = project_root / "scripts" / "update_fixed_cycle_wallet_snapshot.py"

LAST_SNAPSHOT_RUN: dict[str, float] = {}
LAST_SNAPSHOT_LOCK = threading.Lock()
SNAPSHOT_RUN_TTL_SECONDS = 60
SNAPSHOT_IN_PROGRESS: set[str] = set()


def _get_dashboard_long_bots() -> list[dict[str, Any]]:
    return get_available_long_bots()


def _get_dashboard_profile_entries() -> list[dict[str, str]]:
    entries = [{"profile": "main", "display_name": "Main"}]
    entries.extend(
        {"profile": bot["profile"], "display_name": bot["display_name"]}
        for bot in _get_dashboard_long_bots()
    )
    return entries


def _available_profiles() -> list[str]:
    return [entry["profile"] for entry in _get_dashboard_profile_entries()]


def _available_profile_labels() -> dict[str, str]:
    return {entry["profile"]: entry["display_name"] for entry in _get_dashboard_profile_entries()}


def _normalize_dashboard_profile(profile: str | None, *, fallback_to_main: bool = True) -> str | None:
    return normalize_profile(profile, fallback_to_main=fallback_to_main)


def _get_long_bot_by_name(bot_name: str | None) -> dict[str, Any] | None:
    normalized = str(bot_name or "").strip().lower()
    for bot in _get_dashboard_long_bots():
        if bot["bot_name"] == normalized:
            return bot
    return None


def _get_long_bot_for_profile(profile: str | None) -> dict[str, Any] | None:
    normalized = _normalize_dashboard_profile(profile, fallback_to_main=False)
    if not normalized or normalized == "main":
        return None
    return get_long_bot_by_profile(normalized)


def resolve_profile_to_bot_record(profile: str | None) -> dict[str, Any] | None:
    normalized = _normalize_dashboard_profile(profile, fallback_to_main=False)
    if not normalized or normalized == "main":
        return None
    for bot in get_bot_profiles():
        if str(bot.get("profile") or "").strip().lower() == normalized:
            return bot
    raise ValueError(f"Unknown bot profile: {profile}")


def _is_registry_bot_profile(profile: str | None) -> bool:
    try:
        return resolve_profile_to_bot_record(profile) is not None
    except ValueError:
        return False


def _build_dynamic_wallet_snapshot_files() -> dict[str, Path]:
    return {bot["account_name"]: Path(bot["wallet_snapshot_file"]) for bot in _get_dashboard_long_bots()}


def _build_dynamic_bot_state_files() -> dict[str, Path]:
    return {bot["account_name"]: Path(bot["state_file"]) for bot in _get_dashboard_long_bots()}


def _long_bot_shared_scripts_root() -> Path:
    return project_root / "live_bots" / "100_50_hedge_bot" / "shared_scripts"


def _long_bot_shared_script_path(action: str) -> Path | None:
    script_map = {
        "start": _long_bot_shared_scripts_root() / "start_long_bot.sh",
        "stop": _long_bot_shared_scripts_root() / "stop_bot.sh",
        "stop_with_cleanup": _long_bot_shared_scripts_root() / "stop_with_cleanup.sh",
    }
    return script_map.get(action)


def _is_executable_script(path: Path | None) -> bool:
    return bool(path and path.exists() and os.access(path, os.X_OK))


def _validate_long_bot_request(bot_name: str | None) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(bot_name, str) or not re.match(r"^long_bot_[0-9]+$", bot_name or ""):
        return None, "Invalid bot_name"
    bot = _get_long_bot_by_name(bot_name)
    if not bot:
        return None, "Unknown bot_name"
    bot_dir = Path(bot["bot_dir"])
    config_file = Path(bot["config_file"])
    if not bot_dir.exists() or not bot_dir.is_dir():
        return None, f"Bot directory missing: {bot_dir}"
    if not config_file.exists():
        return None, f"Config missing: {config_file}"
    return bot, None


def _is_discovered_long_bot_name(bot_name: str | None) -> bool:
    return _get_long_bot_by_name(bot_name) is not None



from utils.auth import authenticate_user, get_user_role
from dashboard.vendor.core.bybit_order_manager import BybitOrderManager
from utils.bot_monitor import (
    get_all_bots,
    get_bot_status,
    load_bot_state,
    is_bot_running,
    is_any_bot_running,
    get_all_services_status,
    is_master_bot_running,
    find_all_master_bot_processes,
    find_all_master_bot_api_processes,
    is_master_bot_api_running,
    get_bot_pid_from_run_dir,
    get_fixed_cycle_symbol,
    find_fixed_cycle_runner_pid,
)
from utils.config_manager import load_config, save_config, save_config_with_cycles, get_default_config, get_config_path, format_config_with_blocks, get_config_header_comment
from utils.position_info import (
    get_position_info,
    calculate_next_burn_size_from_position,
    has_log_file_changed,
    calculate_rebuy_info,
    parse_position_from_logs,
    get_burn_stats,
    simulate_burn_profit,
    simulate_tp_profit,
)
from utils.notifications import send_ntfy_alert, send_bot_alert
from dashboard.utils.bot_profiles import (
    get_available_long_bots,
    get_long_bot_by_profile,
    is_bot_profile,
    normalize_profile,
    profile_to_account_name,
    profile_to_long_bot_name,
)
try:
    from dashboard.bot_registry import (
        get_closed_pnl_accounts,
        get_bot_paths,
        get_bot_profiles,
        get_dashboard_accounts,
        get_live_charts_accounts,
        resolve_account,
    )
except ImportError:
    from bot_registry import (
        get_closed_pnl_accounts,
        get_bot_paths,
        get_bot_profiles,
        get_dashboard_accounts,
        get_live_charts_accounts,
        resolve_account,
    )


def _serializable_bot_profiles() -> list[dict[str, Any]]:
    return [
        {**profile, "bot_dir": str(profile["bot_dir"])}
        for profile in get_bot_profiles()
    ]

# Import BybitOrderManager für Equity-Endpoint (noch nicht auf Master Bot API migriert)
from core.bybit_order_manager import BybitOrderManager

# Import Order Params Manager für JSON-Datei-Verwaltung
from bots.shared.order_params_manager import delete_order_params
from bots.shared.atr_helper import update_atr_burn_state, load_atr_burn_state

# Master Bot API Configuration
MASTER_BOT_API_URL = os.getenv("MASTER_BOT_API_URL", "http://localhost:8001")
MASTER_BOT_API_TOKEN = os.getenv("MASTER_BOT_API_TOKEN", "superlongrandomstringchangeme")

# Tracks symbols that already issued a bot start via open_hedged_positions.
# NOTE: In-memory only. Persistence can be added here if restart-deduplication is required.
_START_GATE: Dict[str, Dict[str, object]] = {}
_START_GATE_LOCK = threading.Lock()
_BOT_START_IN_PROGRESS: Set[str] = set()
_BOT_START_LOCK = threading.Lock()

# Setup logging with DEBUG level and file handler
log_dir = Path("logs")
log_dir.mkdir(parents=True, exist_ok=True)
dashboard_log_file = log_dir / "dashboard.log"
master_log_file = log_dir / "master.log"

# Beim Neustart: Dashboard-Log leeren (frischer Lauf)
if dashboard_log_file.exists():
    dashboard_log_file.write_text("", encoding="utf-8")

# Create formatter
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Dashboard file handler (DEBUG level - all details)
dashboard_handler = logging.FileHandler(dashboard_log_file, mode="a", encoding="utf-8")
dashboard_handler.setLevel(logging.DEBUG)
dashboard_handler.setFormatter(formatter)

# Master file handler (INFO level, keeps growing)
master_handler = logging.FileHandler(master_log_file, mode="a", encoding="utf-8")
master_handler.setLevel(logging.INFO)
master_handler.setFormatter(formatter)

# Console handler (INFO level - less verbose)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

# Configure root logger
logging.basicConfig(
    level=logging.DEBUG,
    handlers=[dashboard_handler, master_handler, console_handler],
)

logger = logging.getLogger(__name__)
logger.debug("=" * 80)
logger.debug("🚀 Dashboard gestartet - Logging initialisiert")
logger.debug("📁 Dashboard-Log-Datei: %s", dashboard_log_file)
logger.debug("=" * 80)

# Globale Variable für Start-Zeit (für Health-Check)
app_start_time = time.time()

app = FastAPI()

# Cache for position info (cache for 30 seconds to speed up dashboard and reduce API calls)
position_cache = {}
position_cache_timeout = 30  # seconds - erhöht von 10 auf 30, um Rate-Limits zu vermeiden

# Log file modification times for change detection
log_file_mtimes = {}  # {symbol: mtime}

# Circuit Breaker für fehlgeschlagene Endpoints (verhindert endlose Retries)
circuit_breaker = {}  # {endpoint: {'failures': int, 'last_failure': float, 'state': 'closed'|'open'|'half_open'}}
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5  # Nach 5 Fehlern öffnet der Circuit
CIRCUIT_BREAKER_RESET_TIMEOUT = 60  # Nach 60 Sekunden versucht es wieder (half-open)

# Request-Limits (verhindert zu viele gleichzeitige Requests)
active_requests = {}  # {endpoint: count}
MAX_CONCURRENT_REQUESTS = 3  # Max 3 gleichzeitige Requests pro Endpoint
# User-Initiiertes (Update/Config/Laden) – höheres Limit, Klicks sollen sofort reagieren
CONFIG_ENDPOINTS = frozenset({
    "POST /api/hedge/update-config",
    "GET /api/hedge/config-raw",
    "POST /api/hedge/config-raw",
})
MAX_CONCURRENT_CONFIG = 16


def _is_rate_limit_exempt(path: str) -> bool:
    """User-Klicks (Laden/Update/Config/Stop/Start/Restart): kein Rate-Limit, damit sie sofort durchkommen."""
    if path.startswith("/api/bots/") and (
        "/config" in path or path.endswith("/stop") or path.endswith("/start") or "/restart" in path
    ):
        return True
    if path in ("/api/hedge/update-config", "/api/hedge/config-raw"):
        return True
    if path.startswith("/api/hedge/stop-bots/") or path == "/api/hedge/stop-bot-at-price":
        return True
    if (path.startswith("/api/hedge/start-bots/") or path == "/api/hedge/start-bot-at-price"
            or path == "/api/hedge/start-bot-script" or path.startswith("/api/hedge/set-tp-config/")):
        return True
    if path in ("/api/hedge/restart-long-auto", "/api/hedge/restart-short-auto") or path.startswith("/api/hedge/restart-bots/"):
        return True
    if path == "/api/system/restart-all" or path.startswith("/api/system/start/"):
        return True
    if path.startswith("/api/services/") and ("/restart" in path or "/start" in path):
        return True
    return False

# Im Fallback (Logs+Config) nie im Dropdown anzeigen – keine aktive Position mehr / eingestellte Coins
SYMBOLS_EXCLUDED_FROM_DROPDOWN_FALLBACK = frozenset({"LUNAUSDT", "LUNA2USDT"})

_SYMBOLS_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "state" / "symbols.json"


def _load_symbols_state() -> dict:
    """Lädt Symbol-Status (aktive/archivierte Symbole) aus Datei."""
    try:
        if not _SYMBOLS_STATE_FILE.exists():
            return {"active_symbols": [], "archived_symbols": []}
        with open(_SYMBOLS_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("active_symbols", [])
        data.setdefault("archived_symbols", [])
        return data
    except Exception:
        return {"active_symbols": [], "archived_symbols": []}


def _save_symbols_state(data: dict) -> None:
    """Speichert Symbol-Status-Datei best-effort."""
    try:
        _SYMBOLS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_SYMBOLS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except Exception:
        logger.warning("Konnte symbols.json nicht schreiben", exc_info=True)


def _archive_symbol(symbol: str) -> dict:
    """Fügt ein Symbol zur Archiv-Liste hinzu (für Dropdown-Ausblendung) und löscht per-coin Config-Dateien."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"success": False, "message": "symbol fehlt"}
    state = _load_symbols_state()
    active = {s.strip().upper() for s in state.get("active_symbols", [])}
    archived = {s.strip().upper() for s in state.get("archived_symbols", [])}
    active.discard(sym)
    archived.add(sym)
    state["active_symbols"] = sorted(active)
    state["archived_symbols"] = sorted(archived)
    _save_symbols_state(state)

    # Per-Coin Config-Dateien löschen (nur *_config_<SYMBOL>.yaml, globale Templates bleiben erhalten)
    deleted_files: list[str] = []
    project_root = Path(__file__).resolve().parent.parent
    cfg_dir = project_root / "config"
    for prefix in ("long_config_", "short_config_"):
        cfg_path = cfg_dir / f"{prefix}{sym}.yaml"
        try:
            if cfg_path.exists():
                cfg_path.unlink()
                deleted_files.append(str(cfg_path))
        except Exception:
            logger.warning("Konnte Config-Datei nicht löschen: %s", cfg_path, exc_info=True)

    return {
        "success": True,
        "symbol": sym,
        "active_symbols": state["active_symbols"],
        "archived_symbols": state["archived_symbols"],
        "deleted_config_files": deleted_files,
    }


def _unarchive_symbol(symbol: str) -> dict:
    """Entfernt ein Symbol aus der Archiv-Liste."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"success": False, "message": "symbol fehlt"}
    state = _load_symbols_state()
    active = {s.strip().upper() for s in state.get("active_symbols", [])}
    archived = {s.strip().upper() for s in state.get("archived_symbols", [])}
    archived.discard(sym)
    active.add(sym)
    state["active_symbols"] = sorted(active)
    state["archived_symbols"] = sorted(archived)
    _save_symbols_state(state)
    return {"success": True, "symbol": sym, "active_symbols": state["active_symbols"], "archived_symbols": state["archived_symbols"]}

# Templates
templates_dir = Path(__file__).parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)))

# Cache für sudo-Passwort (wird einmal geladen)
_sudo_password_cache = None

# WebSocket-Verbindungen für Echtzeit-Updates
websocket_connections: Set[WebSocket] = set()
position_updates_cache: Dict[str, dict] = {}  # {symbol: {long: {...}, short: {...}}}
# Cache für einzelne Position-Updates (Long und Short getrennt)
position_cache_raw: Dict[str, Dict[int, dict]] = {}  # {symbol: {positionIdx: position_data}}

# Dashboard WS: main account -> long bot, sub account -> short bot
WS_ACCOUNT_TO_BOT_TYPE = {
    "main": "long",
    "sub": "short"
}

# Live price/PnL cache to avoid API overload
LIVE_API_MIN_INTERVAL_SECONDS = 5.0
LIVE_POSITION_CACHE: Dict[str, Dict[str, object]] = {}

# Separate (faster) cache for live-charts tile grid
LIVE_GRID_API_MIN_INTERVAL_SECONDS = 2.0
LIVE_POSITIONS_GRID_CACHE: Dict[str, Dict[str, object]] = {}

def _live_chart_strategy_state_files() -> Dict[str, Path]:
    files = _build_dynamic_bot_state_files()
    files["Short_bot_1"] = project_root / "logs" / "fixed_cycle_state.json"
    return files


def _state_file_for_account(account: str) -> Path | None:
    return _live_chart_strategy_state_files().get(account)


def _wallet_snapshot_file_for_account(account: str) -> Path | None:
    return _build_dynamic_wallet_snapshot_files().get(account)


def _ensure_wallet_snapshot_for_account(account: str) -> None:
    snapshot_path = _wallet_snapshot_file_for_account(account)
    state_file = _build_dynamic_bot_state_files().get(account)
    if not snapshot_path or not state_file:
        return
    if snapshot_path.exists():
        try:
            age = time.time() - snapshot_path.stat().st_mtime
            if age < WALLET_SNAPSHOT_TTL_SECONDS:
                return
        except OSError:
            pass
    if not WALLET_SNAPSHOT_SCRIPT.exists():
        logger.warning(
            "[dashboard] wallet_snapshot_script_missing",
            {"script": str(WALLET_SNAPSHOT_SCRIPT)},
        )
        return
    cmd = [
        sys.executable,
        str(WALLET_SNAPSHOT_SCRIPT),
        "--bot-name",
        account.lower(),
        "--state-file",
        str(state_file),
        "--output-file",
        str(snapshot_path),
        "--mode",
        "flat",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, timeout=65)
        logger.info(
            "[dashboard] wallet_snapshot_script_executed",
            {"account": account, "snapshot_file": str(snapshot_path)},
        )
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "[dashboard] wallet_snapshot_script_failed",
            {
                "account": account,
                "cmd": cmd,
                "stderr": exc.stderr.decode(errors="ignore") if exc.stderr else None,
            },
        )
    except Exception as exc:
        logger.warning(
            "[dashboard] wallet_snapshot_script_error",
            {"account": account, "error": str(exc)},
        )


def get_account_pnl_paths(account: str | None) -> dict[str, Path]:
    resolved = resolve_account(account)
    if resolved and resolved["bot_name"]:
        paths = get_bot_paths(resolved["bot_name"])
        if paths:
            return {
                "runtime_log_path": paths["runtime_log_file"],
                "confirmed_pnl_history_path": paths["confirmed_order_pnl_history_file"],
                "dashboard_closed_pnl_history_path": paths["dashboard_closed_pnl_history_file"],
                "wallet_snapshot_path": paths["snapshot_file"],
            }
    return {
        "runtime_log_path": GLOBAL_RUNTIME_LOG_PATH,
        "confirmed_pnl_history_path": CONFIRMED_ORDER_PNL_HISTORY_FILE,
        "dashboard_closed_pnl_history_path": DASHBOARD_CLOSED_PNL_HISTORY_FILE,
        "wallet_snapshot_path": _build_dynamic_wallet_snapshot_files().get("Long_bot_1")
        or project_root
        / "live_bots"
        / "100_50_hedge_bot"
        / "long_bot_1"
        / "snapshots"
        / "fixed_cycle_wallet_snapshot.json",
    }


def _load_strategy_state_for_account(account: str) -> dict[str, Any] | None:
    state_file = _state_file_for_account(account)
    if not state_file or not state_file.exists():
        return None
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        logger.debug(f"[LIVE-CHARTS] Konnte state file {state_file} nicht laden", exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    return payload.get("strategy_state") or {}


def _safe_wallet_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_wallet_snapshot_payload(state: dict[str, Any], account: str) -> dict[str, Any] | None:
    wallet_current = state.get("wallet_snapshot_current")
    if not isinstance(wallet_current, dict):
        return None
    wallet_previous = state.get("wallet_snapshot_previous") or {}
    bot_name = str(
        wallet_current.get("bot_name") or state.get("bot_name") or account
    )
    return {
        "bot_name": bot_name,
        "symbol": str(wallet_current.get("symbol") or "").upper(),
        "wallet_snapshot_timestamp_utc3": wallet_current.get("timestamp_utc3"),
        "wallet_balance_current_usdt": _safe_wallet_float(wallet_current.get("wallet_balance_usdt")),
        "wallet_balance_previous_usdt": _safe_wallet_float(wallet_previous.get("wallet_balance_usdt")),
        "last_trade_wallet_profit_usdt": _safe_wallet_float(state.get("last_trade_wallet_profit_usdt")),
        "last_trade_wallet_profit_source": state.get("last_trade_wallet_profit_source"),
        "last_trade_wallet_profit_available": bool(state.get("last_trade_wallet_profit_available")),
        "last_trade_wallet_profit_reason": state.get("last_trade_wallet_profit_reason"),
        "last_trade_wallet_profit_timestamp_utc3": state.get("last_trade_wallet_profit_timestamp_utc3"),
    }


def _extract_cycle_pnl_entries(state: dict[str, Any]) -> list[dict[str, Any]]:
    ledger = state.get("audit_pnl_ledger") or {}
    raw_entries = ledger.get("cycle_pnl_entries") or {}
    entries = []
    for entry_key, entry in raw_entries.items():
        try:
            fill_type, cycle_index, _ = str(entry_key).split(":", 2)
        except ValueError:
            continue
        entries.append(
            {
                "fill_type": fill_type,
                "cycle_index": cycle_index,
                "pnl": _safe_wallet_float(entry.get("pnl")) or 0.0,
                "source": entry.get("source"),
                "is_confirmed": bool(entry.get("is_confirmed")),
                "entry_key": entry_key,
            }
        )
    return entries


def _extract_final_exit_pnl(state: dict[str, Any]) -> dict[str, Any]:
    ledger = state.get("audit_pnl_ledger") or {}
    return {
        "final_long_exit_pnl": _safe_wallet_float(ledger.get("final_long_exit_pnl")),
        "final_short_exit_pnl": _safe_wallet_float(ledger.get("final_short_exit_pnl")),
    }


def _load_wallet_snapshot_file(account: str) -> dict[str, Any] | None:
    snapshot_file = _wallet_snapshot_file_for_account(account)
    if not snapshot_file or not snapshot_file.exists():
        return None
    try:
        data = json.loads(snapshot_file.read_text(encoding="utf-8"))
    except Exception:
        logger.debug(f"[LIVE-CHARTS] Konnte wallet snapshot file {snapshot_file} nicht lesen", exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _now_utc3_iso() -> str:
    return datetime.now(timezone(timedelta(hours=3))).isoformat()


def _normalize_account(value: str | None) -> str:
    return str(value or "").strip().lower()


def _load_dashboard_closed_pnl_history(
    account: str | None = None,
    path: Path | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    file_path = path or DASHBOARD_CLOSED_PNL_HISTORY_FILE
    file_exists = file_path.exists()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    normalized_account = _normalize_account(account)
    lines: list[str] = []
    if not file_exists:
        logger.info(
            "[dashboard] dashboard_closed_pnl_history_loaded_for_api",
            {
                "account": account,
                "normalized_account": normalized_account,
                "raw_count": 0,
                "filtered_count": 0,
                "file_exists": False,
                "file_path": str(file_path),
            },
        )
        return []
    try:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        logger.warning(
            "[dashboard] dashboard_closed_pnl_history_load_failed",
            {"file_path": str(file_path)},
            exc_info=True,
        )
        return []
    raw_count = sum(1 for line in lines if line.strip())
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            logger.warning("[dashboard] dashboard_closed_pnl_history_parse_error", {"line": line})
            continue
        if normalized_account:
            if _normalize_account(payload.get("account")) != normalized_account:
                continue
        trade_block_id = payload.get("trade_block_id")
        if not trade_block_id or trade_block_id in seen:
            continue
        seen.add(trade_block_id)
        entries.append(payload)
        if len(entries) >= limit:
            break
    filtered_count = len(entries)
    logger.info(
        "[dashboard] dashboard_closed_pnl_history_loaded_for_api",
        {
            "account": account,
            "normalized_account": normalized_account,
            "raw_count": raw_count,
            "filtered_count": filtered_count,
            "file_exists": file_exists,
            "file_path": str(file_path),
            "reason": "history_loaded",
        },
    )
    if file_exists and raw_count > 0 and filtered_count == 0:
        logger.info(
            "[dashboard] dashboard_closed_pnl_history_empty_for_api",
            {
                "account": account,
                "normalized_account": normalized_account,
                "raw_count": raw_count,
                "filtered_count": filtered_count,
                "reason": "account_filter_removed_all",
            },
        )
    return entries


def load_confirmed_order_pnl_rows(
    account: str | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    file_path = path or CONFIRMED_ORDER_PNL_HISTORY_FILE
    file_exists = file_path.exists()
    raw_count = 0
    rows_by_key: dict[str, dict[str, Any]] = {}
    normalized_account = (account or "").strip().lower()
    if file_exists:
        try:
            for line in file_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                raw_count += 1
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if str(payload.get("source") or "") != "bot_confirmed_pnl":
                    continue
                exchange_order_id = str(payload.get("exchange_order_id") or "").strip()
                purpose = str(payload.get("purpose") or "").strip()
                closed_pnl = payload.get("closed_pnl")
                if not exchange_order_id or not purpose or closed_pnl is None:
                    continue
                bot_name = str(payload.get("bot_name") or payload.get("account") or "").strip()
                if normalized_account and bot_name and bot_name.lower() != normalized_account:
                    continue
                try:
                    normalized_closed_pnl = float(closed_pnl)
                except (TypeError, ValueError):
                    continue
                dedupe_key = str(payload.get("dedupe_key") or f"{exchange_order_id}:{purpose}").strip()
                timestamp = payload.get("timestamp")
                rows_by_key[dedupe_key] = {
                    "orderId": exchange_order_id,
                    "exchange_order_id": exchange_order_id,
                    "orderLinkId": payload.get("client_order_id"),
                    "order_link_id": payload.get("client_order_id"),
                    "symbol": payload.get("symbol"),
                    "closedPnl": normalized_closed_pnl,
                    "updatedTime": timestamp,
                    "tradeTime": timestamp,
                    "createdTime": timestamp,
                    "purpose": purpose,
                    "trade_type": purpose,
                    "source": "bot_confirmed_pnl",
                    "trade_block_id": payload.get("trade_block_id"),
                    "cycle_index": payload.get("cycle_index"),
                    "pnl_scope": payload.get("pnl_scope"),
                    "pnl_source": payload.get("pnl_source"),
                    "dedupe_key": dedupe_key,
                }
        except Exception:
            logger.warning("[dashboard] dashboard_confirmed_order_pnl_rows_load_failed", exc_info=True)
            rows_by_key = {}
    logger.info(
        "[dashboard] dashboard_confirmed_order_pnl_rows_loaded",
        {
            "file_path": str(file_path),
            "file_exists": file_exists,
            "raw_count": raw_count,
            "deduped_count": len(rows_by_key),
            "account": account,
        },
    )
    return list(rows_by_key.values())


def _normalize_confirmed_pnl_row_for_history(row: dict[str, Any]) -> dict[str, Any] | None:
    if not row:
        return None
    order_id = str(row.get("orderId") or row.get("exchange_order_id") or "").strip()
    if not order_id:
        return None
    closed_pnl = row.get("closedPnl")
    if closed_pnl is None:
        return None
    dedupe_key = str(
        row.get("dedupe_key") or f"{order_id}:{row.get('purpose') or row.get('trade_type') or ''}:{closed_pnl}"
    ).strip()
    timestamp = (
        row.get("timestamp")
        or row.get("updatedTime")
        or row.get("tradeTime")
        or row.get("createdTime")
        or row.get("finalized_at")
        or 0
    )
    return {
        "symbol": row.get("symbol"),
        "orderId": order_id,
        "exchange_order_id": row.get("exchange_order_id") or order_id,
        "client_order_id": row.get("orderLinkId") or row.get("order_link_id") or row.get("client_order_id"),
        "order_link_id": row.get("order_link_id") or row.get("orderLinkId"),
        "purpose": row.get("purpose") or row.get("trade_type"),
        "trade_type": row.get("trade_type") or row.get("purpose"),
        "closedPnl": closed_pnl,
        "closed_pnl": closed_pnl,
        "timestamp": timestamp,
        "trade_block_id": row.get("trade_block_id"),
        "cycle_index": row.get("cycle_index"),
        "source": row.get("source") or "confirmed_order_pnl",
        "dedupe_key": dedupe_key,
    }


def _merge_confirmed_order_pnl_rows_into_history(
    closed_history: list[dict[str, Any]],
    confirmed_rows: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    if not confirmed_rows:
        return [], 0
    seen_trade_block_ids = {
        entry.get("trade_block_id")
        for entry in closed_history
        if entry.get("trade_block_id")
    }
    seen_confirmed_keys: set[str] = set()
    merged: list[dict[str, Any]] = []
    duplicates = 0
    for row in confirmed_rows:
        normalized = _normalize_confirmed_pnl_row_for_history(row)
        if normalized is None:
            continue
        dedupe_key = normalized.get("dedupe_key")
        if not dedupe_key:
            continue
        if dedupe_key in seen_confirmed_keys:
            duplicates += 1
            continue
        seen_confirmed_keys.add(dedupe_key)
        tbid = normalized.get("trade_block_id")
        if tbid and tbid in seen_trade_block_ids:
            continue
        merged.append(normalized)
    return merged, duplicates


def _append_dashboard_closed_pnl_history(entry: dict[str, Any]) -> None:
    path = DASHBOARD_CLOSED_PNL_HISTORY_FILE
    trade_block_id = entry.get("trade_block_id")
    if not trade_block_id:
        return
    existing_entries = _load_dashboard_closed_pnl_history(limit=1000)
    ids = {e.get("trade_block_id") for e in existing_entries if e.get("trade_block_id")}
    if trade_block_id in ids:
        logger.info("[dashboard] dashboard_closed_pnl_history_duplicate_skipped", {"trade_block_id": trade_block_id})
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("[dashboard] dashboard_closed_pnl_history_append", {"trade_block_id": trade_block_id})
    except Exception:
        logger.warning("[dashboard] dashboard_closed_pnl_history_append_failed", exc_info=True)


def _extract_dict_from_line(line: str, marker: str) -> dict[str, Any] | None:
    start = line.find("{", line.find(marker))
    if start == -1:
        return None
    depth = 0
    for idx in range(start, len(line)):
        ch = line[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                snippet = line[start: idx + 1]
                try:
                    return ast.literal_eval(snippet)
                except Exception:
                    logger.warning(
                        "[dashboard] dashboard_closed_pnl_history_parse_error",
                        {"snippet": snippet},
                    )
                    return None
    return None


def _persist_closed_pnl_history_from_runtime_log(
    log_path: Path | None = None,
    output_path: Path | None = None,
    account_label: str = "Long_bot_1",
) -> None:
    log_path = log_path or project_root / "logs" / "fixed_cycle_hedge_runtime.log"
    output_path = output_path or DASHBOARD_CLOSED_PNL_HISTORY_FILE
    if not log_path.exists():
        return
    seen_ids = {
        entry.get("trade_block_id")
        for entry in _load_dashboard_closed_pnl_history(path=output_path, limit=1000)
        if entry.get("trade_block_id")
    }
    events = (
        "fixed_cycle_last_trade_pnl_persisted",
        "fixed_cycle_trade_pnl_finalized",
    )
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                for event in events:
                    if event not in line:
                        continue
                    payload = _extract_dict_from_line(line, event)
                    if not payload:
                        continue
                    trade_block_id = payload.get("trade_block_id")
                    total_trade_pnl = payload.get("total_trade_pnl")
                    pnl_complete = payload.get("pnl_complete")
                    if not trade_block_id or total_trade_pnl is None or not pnl_complete:
                        continue
                    if trade_block_id in seen_ids:
                        continue
                    seen_ids.add(trade_block_id)
                    entry = {
                        "account": account_label,
                        "symbol": payload.get("symbol"),
                        "trade_block_id": trade_block_id,
                        "total_trade_pnl": float(total_trade_pnl),
                        "source": payload.get("source"),
                        "pnl_complete": bool(pnl_complete),
                        "finalized_at": payload.get("finalized_at"),
                        "created_at_utc3": payload.get("updated_at_utc3") or _now_utc3_iso(),
                    }
                    breakdown = payload.get("breakdown")
                    if breakdown is not None:
                        entry["breakdown"] = breakdown
                    try:
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        with output_path.open("a", encoding="utf-8") as f_out:
                            f_out.write(json.dumps(entry, ensure_ascii=False) + "\n")
                        logger.info(
                            "[dashboard] dashboard_closed_pnl_history_append",
                            {"trade_block_id": trade_block_id, "account": account_label, "path": str(output_path)},
                        )
                    except Exception:
                        logger.warning(
                            "[dashboard] dashboard_closed_pnl_history_append_failed",
                            {"trade_block_id": trade_block_id, "account": account_label, "path": str(output_path)},
                            exc_info=True,
                        )
    except Exception:
        logger.warning("[dashboard] dashboard_closed_pnl_history_backfill_failed", exc_info=True)


def _load_order_purpose_map_from_runtime_log(
    limit_lines: int = 10000,
    log_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    active_log = log_path or (project_root / "logs" / "fixed_cycle_hedge_runtime.log")
    order_purpose_map: dict[str, dict[str, Any]] = {}
    all_lines: list[str] = []
    try:
        rotated_logs = sorted(
            active_log.parent.glob(f"{active_log.name}.*"),
            key=lambda path: path.stat().st_mtime,
        )
    except Exception:
        rotated_logs = []
    log_files = rotated_logs + [active_log]
    for path in log_files:
        if not path.exists():
            continue
        try:
            all_lines.extend(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        except Exception:
            logger.warning(
                "[dashboard] dashboard_order_purpose_map_log_read_failed",
                {"file": str(path)},
                exc_info=True,
            )
    if limit_lines > 0 and len(all_lines) > limit_lines:
        all_lines = all_lines[-limit_lines:]
    for line in all_lines:
        if "order_submitted" not in line or "exchange_order_id" not in line or "purpose" not in line:
            continue
        start = line.find("{")
        if start == -1:
            continue
        snippet = line[start:]
        payload: dict[str, Any] | None = None
        try:
            loaded = json.loads(snippet)
            if isinstance(loaded, dict):
                payload = loaded
        except Exception:
            try:
                loaded = ast.literal_eval(snippet)
                if isinstance(loaded, dict):
                    payload = loaded
            except Exception:
                payload = None
        if not payload or payload.get("event") != "order_submitted":
            continue
        exchange_order_id = str(payload.get("exchange_order_id") or "").strip()
        purpose = str(payload.get("purpose") or "").strip()
        if not exchange_order_id or not purpose:
            continue
        order_purpose_map[exchange_order_id] = {
            "purpose": purpose,
            "order_link_id": payload.get("order_link_id"),
            "symbol": payload.get("symbol"),
            "timestamp": payload.get("timestamp"),
            "side": payload.get("side"),
            "qty": payload.get("qty"),
            "order_type": payload.get("order_type"),
        }
    return order_purpose_map

def _build_wallet_snapshot_payload_from_file(data: dict[str, Any], account: str) -> dict[str, Any] | None:
    raw_snapshot = data.get("wallet_snapshot")
    if isinstance(raw_snapshot, dict):
        return raw_snapshot
    current_wallet = _safe_wallet_float(
        data.get("current_wallet_usdt") or data.get("wallet_balance_current_usdt")
    )
    if current_wallet is None:
        return None
    start_wallet = _safe_wallet_float(
        data.get("start_wallet_usdt")
        or data.get("next_start_wallet_usdt")
        or data.get("previous_wallet_usdt")
        or data.get("wallet_balance_previous_usdt")
        or data.get("wallet_balance_current_usdt")
    )
    previous_wallet = start_wallet
    raw_profit = data.get("last_trade_profit_usdt")
    if raw_profit is None:
        raw_profit = data.get("last_trade_wallet_profit_usdt")
    profit_value = _safe_wallet_float(raw_profit)
    source = (
        data.get("last_trade_profit_source")
        or data.get("last_trade_wallet_profit_source")
        or data.get("wallet_metric_used")
        or data.get("source")
        or data.get("wallet_balance_source")
    )
    timestamp = (
        data.get("last_trade_profit_timestamp_utc3")
        or data.get("wallet_snapshot_timestamp_utc3")
        or data.get("updated_at_utc3")
    )
    bot_name = str(data.get("bot_name") or account)
    symbol = str(data.get("symbol") or "").upper()
    reason = data.get("last_trade_profit_reason") or data.get("flat_reason") or ""
    return {
        "bot_name": bot_name,
        "symbol": symbol,
        "wallet_snapshot_timestamp_utc3": timestamp,
        "wallet_balance_current_usdt": current_wallet,
        "wallet_balance_previous_usdt": previous_wallet,
        "wallet_balance_start_usdt": start_wallet,
        "last_trade_wallet_profit_usdt": profit_value,
        "last_trade_wallet_profit_source": source,
        "last_trade_wallet_profit_available": bool(data.get("last_trade_profit_available")),
        "last_trade_wallet_profit_reason": reason,
        "last_trade_wallet_profit_timestamp_utc3": timestamp,
    }


def _load_live_wallet_snapshot_for_account(account: str) -> dict[str, Any] | None:
    state = _load_strategy_state_for_account(account)
    snapshot = None
    if state:
        snapshot = _build_wallet_snapshot_payload(state, account)
    if snapshot:
        return snapshot
    file_data = _load_wallet_snapshot_file(account)
    if not file_data:
        return None
    return _build_wallet_snapshot_payload_from_file(file_data, account)


def _bot_profile_for_name(bot_name: str) -> str | None:
    match = re.match(r"^long_bot_(\d+)$", str(bot_name or "").strip().lower())
    if not match:
        return None
    return f"bot_{int(match.group(1))}"


def _fetch_dashboard_bot_pos_qtys(bot_name: str) -> tuple[float | None, float | None, str, bool]:
    profile = _bot_profile_for_name(bot_name)
    if not profile:
        return None, None, "unsupported_bot", True
    try:
        long_key, long_secret, _, _ = _get_account_keys_by_profile(profile)
    except Exception as exc:
        logger.warning("fixed_cycle_wallet_start_snapshot_skipped_flat_unknown %s", exc, exc_info=True)
        return None, None, "bybit_positions", True
    if not long_key or not long_secret:
        logger.info("fixed_cycle_wallet_start_snapshot_skipped_flat_unknown missing keys")
        return None, None, "bybit_positions", True
    try:
        manager = BybitOrderManager(long_key, long_secret)
        positions = manager.fetch_positions_direct(timeout=5)
    except Exception as exc:
        logger.warning("fixed_cycle_wallet_start_snapshot_skipped_flat_unknown %s", exc, exc_info=True)
        return None, None, "bybit_positions", True
    long_qty = 0.0
    short_qty = 0.0
    for pos in positions:
        info = pos.get("info") or {}
        side = (info.get("side") or "").lower()
        size = float(info.get("size") or 0)
        if size <= 0:
            continue
        if side == "buy":
            long_qty += size
        elif side == "sell":
            short_qty += size
    return long_qty, short_qty, "bybit_positions", False


def _extract_position_qtys(state: dict[str, Any]) -> tuple[float | None, float | None]:
    snapshot = state.get("snapshot") or {}
    candidates = [
        (state.get("long_qty"), state.get("short_qty")),
        (state.get("long_size"), state.get("short_size")),
        (snapshot.get("long_qty"), snapshot.get("short_qty")),
        (snapshot.get("long_size"), snapshot.get("short_size")),
        (state.get("strategy_state", {}).get("long_qty"), state.get("strategy_state", {}).get("short_qty")),
    ]
    for long_val, short_val in candidates:
        if long_val is None or short_val is None:
            continue
        try:
            return float(long_val), float(short_val)
        except (TypeError, ValueError):
            continue
    return None, None


def _is_zero_qty(value: float | None) -> bool:
    if value is None:
        return False
    return abs(value) < 1e-9


def _maybe_run_dashboard_start_snapshot(bot_name: str = "long_bot_1", *, project_root: Path | None = None) -> None:
    normalized_bot = (bot_name or "").strip().lower()
    if not normalized_bot:
        return
    now = time.monotonic()
    with LAST_SNAPSHOT_LOCK:
        last = LAST_SNAPSHOT_RUN.get(normalized_bot)
        if normalized_bot in SNAPSHOT_IN_PROGRESS:
            logger.info(
                "fixed_cycle_wallet_start_snapshot_skipped_in_progress",
                {"bot": normalized_bot},
            )
            return
        if last and now - last < SNAPSHOT_RUN_TTL_SECONDS:
            logger.info(
                "fixed_cycle_wallet_start_snapshot_skipped_debounce",
                {
                    "bot": normalized_bot,
                    "ttl_seconds": SNAPSHOT_RUN_TTL_SECONDS,
                    "age_seconds": now - last,
                },
            )
            return
        LAST_SNAPSHOT_RUN[normalized_bot] = now
        SNAPSHOT_IN_PROGRESS.add(normalized_bot)

    long_qty, short_qty, source, failed = _fetch_dashboard_bot_pos_qtys(normalized_bot)
    if failed or long_qty is None or short_qty is None:
        logger.info(
            "fixed_cycle_wallet_start_snapshot_skipped_flat_unknown %s",
            {"bot": bot_name, "source": source},
        )
        return
    if not (_is_zero_qty(long_qty) and _is_zero_qty(short_qty)):
        logger.info(
            "fixed_cycle_wallet_start_snapshot_skipped_not_flat %s",
            {"bot": bot_name, "long_qty": long_qty, "short_qty": short_qty, "source": source},
        )
        return
    root = project_root or Path(__file__).resolve().parent.parent
    script_path = root / "scripts" / "update_fixed_cycle_wallet_snapshot.py"
    python_path = root / ".venv" / "bin" / "python"
    if not script_path.exists():
        logger.warning("fixed_cycle_wallet_snapshot_script_missing %s", {"path": script_path})
        return
    cmd = [
        str(python_path),
        str(script_path),
        "--mode",
        "start",
        "--bot-name",
        bot_name,
        "--long-qty",
        str(long_qty),
        "--short-qty",
        str(short_qty),
        "--state-file",
        str(
            root
            / "live_bots"
            / "100_50_hedge_bot"
            / bot_name
            / "state"
            / "fixed_cycle_state.json"
        ),
        "--output-file",
        str(
            root
            / "live_bots"
            / "100_50_hedge_bot"
            / bot_name
            / "snapshots"
            / "fixed_cycle_wallet_snapshot.json"
        ),
    ]
    logger.info("fixed_cycle_wallet_start_snapshot_started %s", {"cmd": cmd})
    try:
        result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            logger.info(
                "fixed_cycle_wallet_start_snapshot_written %s",
                {
                    "cmd": cmd,
                    "returncode": result.returncode,
                    "stdout": result.stdout.strip(),
                    "stderr": result.stderr.strip(),
                },
            )
            snapshot_file = (
                root
                / "live_bots"
                / "100_50_hedge_bot"
                / bot_name
                / "snapshots"
                / "fixed_cycle_wallet_snapshot.json"
            )
            snapshot_exists = snapshot_file.exists()
            snapshot_meta = {}
            if snapshot_exists:
                try:
                    data = json.loads(snapshot_file.read_text(encoding="utf-8"))
                    snapshot_meta = {
                        "snapshot_phase": data.get("snapshot_phase"),
                        "symbol": data.get("symbol"),
                        "trade_block_id": data.get("trade_block_id"),
                        "updated_at_utc3": data.get("updated_at_utc3"),
                    }
                except Exception:
                    pass
            logger.info(
                "fixed_cycle_wallet_start_snapshot_postcheck",
                {
                    "snapshot_file": str(snapshot_file),
                    "exists": snapshot_exists,
                    **snapshot_meta,
                },
            )
        else:
            logger.error(
                "fixed_cycle_wallet_start_snapshot_failed %s",
                {
                    "cmd": cmd,
                    "returncode": result.returncode,
                    "stdout": result.stdout.strip(),
                    "stderr": result.stderr.strip(),
                },
            )
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "fixed_cycle_wallet_start_snapshot_failed %s",
            {"error": str(exc)},
        )
        logger.info(
            "[dashboard] fixed_cycle_wallet_snapshot_start_timeout",
            {"cmd": cmd, "timeout_seconds": 10},
        )
    except Exception as exc:
        logger.error(
            "fixed_cycle_wallet_start_snapshot_failed %s",
            {"error": str(exc)},
        )
    finally:
        with LAST_SNAPSHOT_LOCK:
            SNAPSHOT_IN_PROGRESS.discard(normalized_bot)


def _maybe_run_dashboard_flat_snapshot(project_root: Path) -> None:
    snapshot_file = (
        project_root
        / "live_bots"
        / "100_50_hedge_bot"
        / "long_bot_1"
        / "snapshots"
        / "fixed_cycle_wallet_snapshot.json"
    )
    if not snapshot_file.exists():
        logger.info("[dashboard] fixed_cycle_wallet_flat_snapshot_skipped_not_start_snapshot", {"reason": "snapshot_missing"})
        return
    try:
        payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[dashboard] fixed_cycle_wallet_flat_snapshot_skipped_not_start_snapshot", {"reason": "parse_failure"})
        return
    if payload.get("snapshot_phase") != "start" or not payload.get("start_wallet_usdt"):
        if payload.get("snapshot_phase") == "flat_exit" and payload.get("last_trade_profit_available"):
            logger.info("[dashboard] fixed_cycle_wallet_flat_snapshot_skipped_already_flat_exit", {"snapshot_phase": payload.get("snapshot_phase")})
            return
        logger.info("[dashboard] fixed_cycle_wallet_flat_snapshot_skipped_not_start_snapshot", {"snapshot_phase": payload.get("snapshot_phase")})
        return
    long_qty, short_qty, source, failed = _fetch_dashboard_bot_pos_qtys("long_bot_1")
    if failed or long_qty is None or short_qty is None:
        logger.info("[dashboard] fixed_cycle_wallet_flat_snapshot_skipped_not_flat", {"reason": "qty_unknown"})
        return
    if not (_is_zero_qty(long_qty) and _is_zero_qty(short_qty)):
        logger.info(
            "[dashboard] fixed_cycle_wallet_flat_snapshot_skipped_not_flat",
            {"long_qty": long_qty, "short_qty": short_qty},
        )
        return
    cmd = [
        str(project_root / ".venv" / "bin" / "python"),
        str(project_root / "scripts" / "update_fixed_cycle_wallet_snapshot.py"),
        "--mode",
        "flat",
        "--bot-name",
        "long_bot_1",
        "--long-qty",
        "0.0",
        "--short-qty",
        "0.0",
    ]
    logger.info("[dashboard] fixed_cycle_wallet_flat_snapshot_started", {"cmd": cmd})
    try:
        result = subprocess.run(cmd, cwd=project_root, capture_output=True, text=True, timeout=10)
        logger.info(
            "[dashboard] fixed_cycle_wallet_flat_snapshot_written",
            {
                "cmd": cmd,
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            },
        )
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "[dashboard] fixed_cycle_wallet_flat_snapshot_timeout",
            {"error": str(exc)},
        )
    except Exception as exc:
        logger.error(
            "[dashboard] fixed_cycle_wallet_flat_snapshot_failed",
            {"error": str(exc)},
        )
    finally:
        if snapshot_file.exists():
            try:
                payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            logger.info(
                "[dashboard] fixed_cycle_wallet_flat_snapshot_postcheck",
                {
                    "snapshot_phase": payload.get("snapshot_phase"),
                    "trade_block_id": payload.get("trade_block_id"),
                    "start_wallet_usdt": payload.get("start_wallet_usdt"),
                    "current_wallet_usdt": payload.get("current_wallet_usdt"),
                    "last_trade_profit_usdt": payload.get("last_trade_profit_usdt"),
                    "last_trade_profit_available": payload.get("last_trade_profit_available"),
                    "last_trade_profit_source": payload.get("last_trade_profit_source"),
                    "updated_at_utc3": payload.get("updated_at_utc3"),
                },
            )

def _load_dashboard_config() -> dict:
    """Load dashboard config, preferring the live hedge-bot config."""
    try:
        project_root = Path(__file__).parent.parent
        config_candidates = [
            project_root / "live_bots" / "100_50_hedge_bot" / "config" / "config.yaml",
            project_root / "config" / "config.yaml",
        ]
        for config_file in config_candidates:
            if not config_file.exists():
                continue
            import yaml
            with open(config_file, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                return loaded
    except Exception:
        return {}
    return {}

def _get_account_keys(account: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve API keys from config/config.yaml."""
    try:
        config = _load_dashboard_config()
        bot_accounts = tuple(
            [bot["account_name"] for bot in _get_dashboard_long_bots()]
            + [f"Short_bot_{bot['bot_number']}" for bot in _get_dashboard_long_bots()]
        )
        if account in bot_accounts:
            account_cfg = config.get(account) if isinstance(config.get(account), dict) else {}
        elif account == "main":
            account_cfg = (
                config.get("master")
                or config.get("Main_bot")
                or config.get("main_bot")
                or config.get("main_account")
                or {}
            )
            if not account_cfg:
                account_cfg = {
                    "api_key": config.get("api_key"),
                    "secret_key": config.get("secret_key")
                }
        else:
            account_cfg = config.get("sub") or config.get("sub_account") or {}
        api_key = str((account_cfg or {}).get("api_key") or "").strip()
        secret_key = str((account_cfg or {}).get("secret_key") or "").strip()
        if api_key and secret_key:
            return api_key, secret_key
        return None, None
    except Exception:
        return None, None


def _get_account_keys_by_profile(profile: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Keys für Long- und Short-Account eines Profils aus config.yaml.
    profile: main|bot_N. profiles[profile].long_account / short_account -> API-Keys.
    Returns: (long_api_key, long_secret, short_api_key, short_secret)
    """
    profile = _normalize_dashboard_profile(profile, fallback_to_main=True)
    if profile not in ("main",) and not is_bot_profile(profile):
        return _get_account_keys("main")[0], _get_account_keys("main")[1], _get_account_keys("sub")[0], _get_account_keys("sub")[1]
    try:
        config = _load_dashboard_config()
        profiles = config.get("profiles") or {}
        prof = profiles.get(profile) if isinstance(profiles, dict) else None
        if not prof:
            return _get_account_keys("main")[0], _get_account_keys("main")[1], _get_account_keys("sub")[0], _get_account_keys("sub")[1]
        long_name = (prof.get("long_account") or "master").strip()
        short_name = (prof.get("short_account") or "sub").strip()
        la, ls = _get_account_keys(long_name)
        sa, ss = _get_account_keys(short_name)
        return la, ls, sa, ss
    except Exception:
        return _get_account_keys("main")[0], _get_account_keys("main")[1], _get_account_keys("sub")[0], _get_account_keys("sub")[1]

def _get_account_keys_for_ws(profile: Optional[str], account: str) -> tuple[Optional[str], Optional[str]]:
    """
    API-Keys für WebSocket: Bei profile bot_N → main=Long-Account, sub=Short-Account.
    Sonst → klassisch master/sub.
    """
    profile = _normalize_dashboard_profile(profile, fallback_to_main=False)
    if profile and is_bot_profile(profile):
        long_key, long_sec, short_key, short_sec = _get_account_keys_by_profile(profile)
        if account == "main":
            return long_key, long_sec
        return short_key, short_sec
    return _get_account_keys(account)


def _get_live_positions_snapshot(account: str, symbol: str, profile: Optional[str] = None) -> dict:
    """Fetch live long+short position data with throttling to avoid API overload."""
    prof = (profile or "").strip()
    cache_key = f"{prof}:{account}:{symbol}" if prof in ("bot_1", "bot_2") else f"{account}:{symbol}"
    now = time.time()

    cached = LIVE_POSITION_CACHE.get(cache_key)
    if cached and now - cached.get("ts", 0) < LIVE_API_MIN_INTERVAL_SECONDS:
        return cached.get("data") or {"long": None, "short": None, "current_price": None}

    api_key, secret_key = _get_account_keys_for_ws(profile, account)
    if not api_key or not secret_key:
        return {"long": None, "short": None, "current_price": None}

    try:
        order_manager = BybitOrderManager(api_key, secret_key)
        positions = order_manager.fetch_positions_direct(symbol, timeout=5)
    except Exception:
        positions = []

    live = {"long": None, "short": None, "current_price": None}
    for pos in positions:
        info = pos.get("info", {})
        side = info.get("side", "")
        size = float(info.get("size", 0) or 0)
        pos_idx = int(info.get("positionIdx", 0) or 0)
        if size <= 0:
            continue

        entry_price = float(info.get("avgPrice") or info.get("entryPrice") or 0)
        mark_price = float(info.get("markPrice") or 0)
        unrealised_pnl = float(info.get("unrealisedPnl") or 0)
        realised_raw = (
            info.get("curRealisedPnl")
            or info.get("cumRealisedPnl")
            or info.get("realisedPnl")
            or info.get("closedPnl")
            or 0
        )
        try:
            realised_pnl = float(realised_raw)
        except (ValueError, TypeError):
            realised_pnl = 0.0

        if not live["current_price"] and mark_price > 0:
            live["current_price"] = mark_price

        position_value_raw = info.get("positionValue")
        try:
            position_value = float(position_value_raw) if position_value_raw is not None and position_value_raw != "" else None
        except (ValueError, TypeError):
            position_value = None

        if side == "Buy" and (pos_idx == 1 or pos_idx == 0) and live["long"] is None:
            live["long"] = {
                "size": size,
                "entry_price": entry_price,
                "current_price": mark_price,
                "unrealised_pnl": unrealised_pnl,
                "realised_pnl": realised_pnl,
                "position_value": position_value
            }
        elif side == "Sell" and (pos_idx == 2 or pos_idx == 0) and live["short"] is None:
            live["short"] = {
                "size": size,
                "entry_price": entry_price,
                "current_price": mark_price,
                "unrealised_pnl": unrealised_pnl,
                "realised_pnl": realised_pnl,
                "position_value": position_value
            }

    LIVE_POSITION_CACHE[cache_key] = {"ts": now, "data": live}
    return live


def _collect_symbols_from_bybit(profile: Optional[str]) -> list[str]:
    """Return symbols having open positions via BybitOrderManager (used for fallback)."""
    managers: list[tuple[str, str]] = []
    if profile and profile in ("bot_1", "bot_2", "main"):
        long_key, long_sec, short_key, short_sec = _get_account_keys_by_profile(profile)
        if long_key and long_sec:
            managers.append((long_key, long_sec))
        if short_key and short_sec:
            managers.append((short_key, short_sec))
    else:
        main_key, main_sec = _get_account_keys("main")
        sub_key, sub_sec = _get_account_keys("sub")
        if main_key and main_sec:
            managers.append((main_key, main_sec))
        if sub_key and sub_sec:
            managers.append((sub_key, sub_sec))

    def _fetch(symbol: Optional[str]) -> set[str]:
        result: set[str] = set()
        if not managers:
            return result
        for api_key, secret_key in managers:
            try:
                om = BybitOrderManager(api_key, secret_key)
                positions = om.fetch_positions_direct(symbol, 5) or []
            except Exception as exc:
                logger.debug("symbols-bybit-fallback: error fetching positions for key=%s symbol=%s: %s",
                             api_key[:6], symbol or "ALL", exc)
                continue
            for pos in positions:
                info = pos.get("info", {}) or pos
                sym = (info.get("symbol") or "").strip().upper()
                size = float(info.get("size", 0) or 0)
                if sym and size > 0:
                    result.add(sym)
        return result

    symbols = _fetch(None)
    if symbols:
        return sorted(symbols)

    candidates: set[str] = set(_list_symbols_from_dropdown_config_sources(profile=profile))
    candidates.update(_list_symbols_from_logs("long"))
    candidates.update(_list_symbols_from_logs("short"))
    candidates.update(_list_symbols_from_config())
    candidates.update(_symbols_from_dashboard_log())
    current_symbol = _get_current_symbol_from_config()
    if current_symbol:
        candidates.add(current_symbol)
    if not candidates:
        return []

    for sym in candidates:
        symbols.update(_fetch(sym))
    return sorted(symbols)

async def _fallback_symbols_from_bybit(profile: Optional[str], reason: str) -> dict | None:
    symbols = await asyncio.to_thread(_collect_symbols_from_bybit, profile)
    if not symbols:
        return None
    logger.info("[API] /api/hedge/symbols fallback to Bybit positions (%s): %s", reason, symbols)
    return {
        "success": True,
        "symbols": symbols,
        "count": len(symbols),
        "hedge_count": len(symbols),
        "debug": {"source": "bybit_positions_fallback", "reason": reason},
    }


def _get_live_positions_all_snapshot(account: str, profile: Optional[str] = None) -> dict:
    """
    Fetch live positions for ALL symbols of an account (throttled).
    profile: main|bot_1|bot_2 – bei bot_1/bot_2 → main=Long-Account, sub=Short-Account.
    Returns: { "SYMBOL": { "symbol": "...", "long": {...}|None, "short": {...}|None, "current_price": float|None } }
    """
    prof = (profile or "").strip()
    cache_key = f"{prof}:{account}:__ALL__" if prof in ("bot_1", "bot_2") else f"{account}:__ALL__"
    now = time.time()
    cached = LIVE_POSITION_CACHE.get(cache_key)
    if cached and now - cached.get("ts", 0) < LIVE_API_MIN_INTERVAL_SECONDS:
        return cached.get("data") or {}

    api_key, secret_key = _get_account_keys_for_ws(profile if prof in ("bot_1", "bot_2") else None, account)
    if not api_key or not secret_key:
        LIVE_POSITION_CACHE[cache_key] = {"ts": now, "data": {}}
        return {}

    try:
        order_manager = BybitOrderManager(api_key, secret_key)
        positions = order_manager.fetch_positions_direct(None, timeout=5)
    except Exception:
        positions = []

    out: dict = {}
    for pos in positions or []:
        info = pos.get("info", {}) or {}
        sym = str(info.get("symbol") or "").strip().upper()
        if not sym:
            continue
        try:
            size = float(info.get("size", 0) or 0)
        except Exception:
            size = 0.0
        if size <= 0:
            continue

        side = str(info.get("side") or "")
        try:
            pos_idx = int(info.get("positionIdx", 0) or 0)
        except Exception:
            pos_idx = 0

        try:
            entry_price = float(info.get("avgPrice") or info.get("entryPrice") or 0) or 0.0
        except Exception:
            entry_price = 0.0
        try:
            mark_price = float(info.get("markPrice") or 0) or 0.0
        except Exception:
            mark_price = 0.0
        try:
            unrealised_pnl = float(info.get("unrealisedPnl") or 0) or 0.0
        except Exception:
            unrealised_pnl = 0.0
        realised_raw = (
            info.get("curRealisedPnl")
            or info.get("cumRealisedPnl")
            or info.get("realisedPnl")
            or info.get("closedPnl")
            or 0
        )
        try:
            realised_pnl = float(realised_raw)
        except (ValueError, TypeError):
            realised_pnl = 0.0

        position_value_raw = info.get("positionValue")
        try:
            position_value = float(position_value_raw) if position_value_raw is not None and position_value_raw != "" else None
        except (ValueError, TypeError):
            position_value = None

        rec = out.get(sym)
        if not rec:
            rec = {"symbol": sym, "long": None, "short": None, "current_price": None}
            out[sym] = rec
        if not rec.get("current_price") and mark_price > 0:
            rec["current_price"] = mark_price

        if side == "Buy" and (pos_idx == 1 or pos_idx == 0) and rec.get("long") is None:
            rec["long"] = {
                "size": size,
                "entry_price": entry_price,
                "current_price": mark_price,
                "unrealised_pnl": unrealised_pnl,
                "realised_pnl": realised_pnl,
                "position_value": position_value,
            }
        elif side == "Sell" and (pos_idx == 2 or pos_idx == 0) and rec.get("short") is None:
            rec["short"] = {
                "size": size,
                "entry_price": entry_price,
                "current_price": mark_price,
                "unrealised_pnl": unrealised_pnl,
                "realised_pnl": realised_pnl,
                "position_value": position_value,
            }

    LIVE_POSITION_CACHE[cache_key] = {"ts": now, "data": out}
    return out

def _list_symbols_from_logs(bot_type: str) -> List[str]:
    """List symbols based on bot log files in data/logs."""
    project_root = Path(__file__).parent.parent
    log_dir = project_root / "data" / "logs"
    if not log_dir.exists():
        return []

    symbols = []
    for log_file in log_dir.glob(f"{bot_type}_bot_*.log"):
        parts = log_file.stem.split("_")
        if len(parts) >= 3:
            symbols.append(parts[-1])
    return sorted(set(symbols))


def _symbols_from_dashboard_log() -> List[str]:
    """Extract symbols referenced by the dashboard in recent API calls."""
    log_paths = [
        Path(__file__).resolve().parent / "logs" / "dashboard.log",
        Path("/tmp/dashboard.log"),
    ]
    symbols: set[str] = set()
    pattern = re.compile(r"/api/hedge/positions/([A-Z0-9]+)")
    for log_file in log_paths:
        if not log_file.exists():
            continue
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    match = pattern.search(line)
                    if match:
                        symbols.add(match.group(1).upper())
        except Exception:
            continue
    return sorted(symbols)


def _ensure_fixed_cycle_long_bot_status(bots_by_symbol: dict, profile: Optional[str] = None):
    """Inject strict fixed-cycle long-bot status for the selected bot_N profile."""
    prof = _normalize_dashboard_profile(profile, fallback_to_main=False)
    if not prof or not is_bot_profile(prof):
        return
    try:
        from utils.bot_monitor import get_bot_status

        bot_name = profile_to_long_bot_name(prof)
        if not bot_name:
            return
        bot_status = get_bot_status("", bot_type="long", bot_name=bot_name, profile=prof)
        symbol = str(bot_status.get("symbol") or get_fixed_cycle_symbol() or "").strip().upper()
        if not symbol:
            return
        if symbol not in bots_by_symbol:
            bots_by_symbol[symbol] = {}
        long_entry = dict(bots_by_symbol[symbol].get("long", {}))
        if bot_status.get("status_label") and not bot_status.get("status_text"):
            bot_status["status_text"] = bot_status.get("status_label")
        merged = {}
        merged.update(long_entry)
        merged.update(bot_status)
        if not merged.get("service_name"):
            merged["service_name"] = long_entry.get("service_name") or "fixed_cycle_hedge_bot.runner"
        bots_by_symbol[symbol]["long"] = merged
    except Exception as exc:
        logger.debug("[SYSTEM-STATUS] Strict fixed-cycle long status could not be injected: %s", exc)


def _inject_profile_long_bot_runtime_status(bots_by_symbol: dict, profile: Optional[str] = None) -> Optional[str]:
    """Inject current profile long-bot runtime status from run/status.json for bot_N."""
    prof = _normalize_dashboard_profile(profile, fallback_to_main=False)
    if not prof or not is_bot_profile(prof):
        return None
    try:
        from utils.bot_monitor import get_bot_status

        bot_name = profile_to_long_bot_name(prof)
        if not bot_name:
            return None
        bot_status = get_bot_status("", bot_type="long", bot_name=bot_name, profile=prof)
        symbol = str(bot_status.get("symbol") or "").strip().upper()
        if not symbol:
            return None
        if symbol not in bots_by_symbol:
            bots_by_symbol[symbol] = {}
        if bot_status.get("status_label") and not bot_status.get("status_text"):
            bot_status["status_text"] = bot_status.get("status_label")
        bots_by_symbol[symbol]["long"] = bot_status
        return symbol
    except Exception as exc:
        logger.debug("[SYSTEM-STATUS] Profil-Long-Status konnte nicht injiziert werden: %s", exc)
        return None


def _inject_fixed_cycle_overview_entry(bots_list: list[dict], prof_key: str):
    """Ensure bot_N overview uses strict registry-backed long-bot status."""
    if not is_bot_profile(prof_key):
        return
    try:
        from utils.bot_monitor import get_bot_status

        bot_name = profile_to_long_bot_name(prof_key)
        if not bot_name:
            return
        bot_status = get_bot_status("", bot_type="long", bot_name=bot_name, profile=prof_key)
    except Exception as exc:
        logger.debug("[ALL-BOTS-OVERVIEW] Strict long status injection failed for %s: %s", prof_key, exc)
        return
    symbol = str(bot_status.get("symbol") or get_fixed_cycle_symbol() or "").strip().upper()
    if not symbol:
        return
    running = bool(bot_status.get("running"))
    for bot in bots_list:
        if bot.get("symbol") == symbol:
            bot["long"] = running
            return
    bots_list.insert(0, {
        "symbol": symbol,
        "long": running,
        "short": False
    })


def _get_profile_scoped_long_status(symbol: str, profile: Optional[str] = None) -> dict:
    """Resolve long-bot status strictly for bot_N profiles, generic otherwise."""
    prof = _normalize_dashboard_profile(profile, fallback_to_main=False)
    if prof and is_bot_profile(prof):
        try:
            from utils.bot_monitor import get_bot_status

            bot_name = profile_to_long_bot_name(prof)
            if bot_name:
                bot_status = get_bot_status(symbol, bot_type="long", bot_name=bot_name, profile=prof)
                if bot_status.get("status_label") and not bot_status.get("status_text"):
                    bot_status["status_text"] = bot_status.get("status_label")
                if not bot_status.get("service_name"):
                    bot_status["service_name"] = "fixed_cycle_hedge_bot.runner"
                return bot_status
        except Exception as exc:
            logger.debug("[SYSTEM-STATUS] Profile-scoped long status fallback for %s failed: %s", prof, exc)

    running = is_bot_running(symbol, bot_type="long", profile=prof or None)
    return {
        "running": running,
        "status": "running" if running else "stopped",
        "status_text": "Long Bot läuft" if running else "Long Bot gestoppt",
        "service_name": f"hedgebot-long@{symbol}" if symbol else "hedgebot-long",
    }


def _list_symbols_from_config() -> List[str]:
    """Symbol aus long_config.yaml / short_config.yaml (Fallback wenn Logs leer)."""
    project_root = Path(__file__).resolve().parent.parent
    out = []
    for name in ("long_config.yaml", "short_config.yaml"):
        path = project_root / "config" / name
        if not path.exists():
            continue
        try:
            with open(path, "r") as f:
                data = yaml.safe_load(f) or {}
            s = (data.get("symbol") or "").strip().upper()
            if s:
                out.append(s)
        except Exception:
            pass
    return sorted(set(out))


def _list_symbols_from_symbol_config_files(profile: Optional[str] = None) -> List[str]:
    """Symbole aus per-coin Config-Dateien. Bei profile=bot_1/bot_2 nur aus config/bot_1/ bzw. bot_2/."""
    project_root = Path(__file__).resolve().parent.parent
    base_dir = project_root / "config"
    prof = (profile or "").strip().lower()
    cfg_dir = base_dir / prof if prof in ("bot_1", "bot_2") else base_dir
    out: set[str] = set()
    if not cfg_dir.exists():
        return []
    try:
        for prefix in ("long_config_", "short_config_"):
            for p in cfg_dir.glob(f"{prefix}*.yaml"):
                # e.g. long_config_DOGEUSDT.yaml -> DOGEUSDT
                stem = p.stem  # without .yaml
                suffix = stem[len(prefix) :] if stem.startswith(prefix) else ""
                sym = (suffix or "").strip().upper()
                if sym:
                    out.add(sym)
    except Exception:
        # best-effort: dropdown should still work
        return []
    return sorted(out)


def _list_symbols_from_dropdown_config_sources(profile: Optional[str] = None) -> List[str]:
    """Union aus Config-Dateien (für Dropdown-Fallback). Bei profile=bot_1/bot_2 nur Profil-Symbole."""
    prof = (profile or "").strip().lower()
    if prof in ("bot_1", "bot_2"):
        base = set(_list_symbols_from_symbol_config_files(profile=prof))
    else:
        base = set(_list_symbols_from_config() + _list_symbols_from_symbol_config_files(profile=None))
    state = _load_symbols_state()
    archived = {s.strip().upper() for s in state.get("archived_symbols", [])}
    # Entferne archivierte Symbole und hart-exkludierte Symbole
    return sorted(s for s in base if s not in archived and s not in SYMBOLS_EXCLUDED_FROM_DROPDOWN_FALLBACK)


def _get_current_symbol_from_config() -> Optional[str]:
    """Aktuelles Symbol aus Config/Per-Coin-Configs (für Start nach Login)."""
    symbols = _list_symbols_from_dropdown_config_sources()
    return symbols[0] if symbols else None

def _build_tp_sl_orders(tp_price: float | None, sl_price: float | None) -> dict:
    """Build TP/SL order summary data for UI compatibility."""
    return {
        "tp_count": 1 if tp_price else 0,
        "sl_count": 1 if sl_price else 0,
        "tp_prices": [tp_price] if tp_price else [],
        "sl_prices": [sl_price] if sl_price else [],
        "tp_orders": [],
        "sl_orders": []
    }

def _build_side_data(symbol: str, side: str, position_info: dict, bot_type: str, live_data: Optional[dict] = None) -> dict:
    """Build UI-compatible side data using log-based info only."""
    side_data = position_info.get(side, {}) if position_info else {}
    size = side_data.get("size") or (live_data.get("size") if live_data else None)
    entry_price = side_data.get("entry_price") or (live_data.get("entry_price") if live_data else None)
    tp_price = side_data.get("tp_price")
    sl_price = side_data.get("sl_price")
    current_price = (live_data.get("current_price") if live_data else None) or (position_info.get("current_price") if position_info else None)
    position_value = live_data.get("position_value") if live_data else None

    if size and entry_price:
        try:
            size = float(size)
            entry_price = float(entry_price)
        except (ValueError, TypeError):
            return {"exists": False}

        # Use entry price as fallback for current price (log-only mode)
        if not current_price:
            current_price = entry_price

        unrealised_pnl = live_data.get("unrealised_pnl") if live_data else None
        realised_pnl = live_data.get("realised_pnl") if live_data else None

        if side == "long":
            if unrealised_pnl is None:
                unrealised_pnl = (current_price - entry_price) * size
            price_deviation = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
        else:
            if unrealised_pnl is None:
                unrealised_pnl = (entry_price - current_price) * size
            price_deviation = ((entry_price - current_price) / entry_price * 100) if entry_price > 0 else 0

        bot_state = load_bot_state(symbol, bot_type=bot_type)
        burn_count = bot_state.get("burn_count", 0)
        burns_before_rebuy = bot_state.get("burns_before_rebuy", 4)

        size_usdt = position_value if position_value is not None else size * entry_price
        return {
            "size_coins": round(size, 6),
            "size_usdt": round(size_usdt, 6),
            "entry_price": round(entry_price, 6),
            "current_price": round(current_price, 6),
            "unrealised_pnl": round(unrealised_pnl, 6),
            "realised_pnl": round(realised_pnl, 6) if realised_pnl is not None else 0.0,
            "pnl_percentage": round((unrealised_pnl / (size * entry_price)) * 100, 2) if entry_price > 0 else 0,
            "price_deviation": round(price_deviation, 2),
            "tp_price": round(tp_price, 6) if tp_price else None,
            "tp_percentage": None,
            "remaining_to_tp": None,
            "burn_count": burn_count,
            "burns_before_rebuy": burns_before_rebuy,
            "tp_sl_orders": _build_tp_sl_orders(tp_price, sl_price),
            "exists": True
        }

    return {"exists": False}

def _build_ws_payload_for_account(symbol: str, account: str, profile: Optional[str] = None) -> dict:
    """Build WS payload for a specific account using live API. profile=bot_1/bot_2 für Long_bot_1/Short_bot_1."""
    live = _get_live_positions_snapshot(account, symbol, profile=profile) or {}
    long_live = live.get("long")
    short_live = live.get("short")

    current_price = live.get("current_price") or 0.0
    long_data = {"exists": False}
    short_data = {"exists": False}

    if long_live:
        long_data = _build_side_data(symbol, "long", {}, bot_type="long", live_data=long_live)
    if short_live:
        short_data = _build_side_data(symbol, "short", {}, bot_type="short", live_data=short_live)

    # Wie Sub: Beide Karten eines Accounts zeigen denselben Burn. Main = Long-Bot, Sub = Short-Bot.
    long_bot_state = load_bot_state(symbol, bot_type="long")
    short_bot_state = load_bot_state(symbol, bot_type="short")
    long_burn = long_bot_state.get("burn_count", 0)
    long_rebuy = long_bot_state.get("burns_before_rebuy", 4)
    short_burn = short_bot_state.get("burn_count", 0)
    short_rebuy = short_bot_state.get("burns_before_rebuy", 4)
    if account == "main":
        # Main = Long-Bot-Account → beide Karten (Long + Short) mit Long-Bot-Burn (wie Sub beide mit Short-Bot-Burn)
        long_data["burn_count"] = long_burn
        long_data["burns_before_rebuy"] = long_rebuy
        short_data["burn_count"] = long_burn
        short_data["burns_before_rebuy"] = long_rebuy
        main_long_burn = {"burn_count": long_burn, "burns_before_rebuy": long_rebuy}
        main_short_burn = {"burn_count": long_burn, "burns_before_rebuy": long_rebuy}
    else:
        # Sub = Short-Bot-Account → beide Karten mit Short-Bot-Burn
        long_data["burn_count"] = short_burn
        long_data["burns_before_rebuy"] = short_rebuy
        short_data["burn_count"] = short_burn
        short_data["burns_before_rebuy"] = short_rebuy
        main_long_burn = {"burn_count": short_burn, "burns_before_rebuy": short_rebuy}
        main_short_burn = {"burn_count": short_burn, "burns_before_rebuy": short_rebuy}

    total_pnl = 0.0
    if long_data.get("exists"):
        total_pnl += long_data.get("unrealised_pnl", 0)
    if short_data.get("exists"):
        total_pnl += short_data.get("unrealised_pnl", 0)
    return {
        "success": True,
        "symbol": symbol,
        "current_price": round(current_price, 6) if current_price else 0.0,
        "long": long_data,
        "short": short_data,
        "main_long_burn": main_long_burn,
        "main_short_burn": main_short_burn,
        "total_pnl": round(total_pnl, 2)
    }

def get_sudo_password() -> str:
    """
    Lädt das sudo-Passwort aus config.yaml (mit Caching für Performance).
    Falls nicht gefunden, wird 'telgenbuescher' als Fallback verwendet.
    """
    global _sudo_password_cache
    if _sudo_password_cache is not None:
        return _sudo_password_cache
    
    try:
        project_root = Path(__file__).parent.parent
        config_file = project_root / "config/config.yaml"
        if config_file.exists():
            import yaml
            with open(config_file, 'r') as f:
                config = yaml.safe_load(f)
                if config and 'system' in config and 'sudo_password' in config['system']:
                    _sudo_password_cache = config['system']['sudo_password']
                    return _sudo_password_cache
    except Exception as e:
        logger.warning(f"⚠️ Konnte sudo-Passwort nicht aus config.yaml laden: {e}")
    
    # Fallback
    _sudo_password_cache = "telgenbuescher"
    return _sudo_password_cache

# Middleware für globales Error-Handling und Timeouts
@app.middleware("http")
async def timeout_and_error_handler(request: Request, call_next):
    """Globales Error-Handling und Timeout-Schutz"""
    start_time = time.time()
    endpoint = f"{request.method} {request.url.path}"
    
    try:
        # Prüfe Circuit Breaker (außer für /api/system/status - dieser Endpunkt sollte nie blockiert werden)
        if endpoint != "GET /api/system/status" and endpoint in circuit_breaker:
            cb = circuit_breaker[endpoint]
            if cb['state'] == 'open':
                # Circuit ist offen - prüfe ob Reset-Zeit abgelaufen ist
                if time.time() - cb['last_failure'] > CIRCUIT_BREAKER_RESET_TIMEOUT:
                    cb['state'] = 'half_open'
                    logger.info(f"🔄 Circuit Breaker für {endpoint} → half_open (Reset-Timeout abgelaufen)")
                else:
                    # Circuit ist noch offen - gebe sofort Fehler zurück
                    logger.warning(f"🚫 Circuit Breaker für {endpoint} ist OPEN - Request blockiert")
                    # Prüfe ob es ein API-Endpoint ist (JSON) oder HTML
                    if request.url.path.startswith('/api/'):
                        return JSONResponse(
                            status_code=503,
                            content={
                                "success": False,
                                "error": "Service temporarily unavailable (circuit breaker open)",
                                "endpoint": endpoint
                            }
                        )
                    else:
                        # HTML-Endpoint → zeige Fehlerseite
                        return HTMLResponse(
                            status_code=503,
                            content=f"<html><body><h1>Service temporarily unavailable</h1><p>Circuit breaker is open for {endpoint}. Please try again later.</p></body></html>"
                        )
        
        # Prüfe Request-Limit (User-Actions: Laden/Update/Config komplett freigestellt)
        counted = False
        if not _is_rate_limit_exempt(request.url.path):
            if endpoint not in active_requests:
                active_requests[endpoint] = 0
            is_user_action = (
                endpoint in CONFIG_ENDPOINTS
                or (request.url.path.startswith("/api/bots/") and "/config" in request.url.path)
            )
            limit = MAX_CONCURRENT_CONFIG if is_user_action else MAX_CONCURRENT_REQUESTS
            if active_requests[endpoint] >= limit:
                logger.warning(f"⚠️ Request-Limit erreicht für {endpoint} ({active_requests[endpoint]}/{limit})")
                if request.url.path.startswith('/api/'):
                    return JSONResponse(
                        status_code=429,
                        content={
                            "success": False,
                            "error": "Too many concurrent requests",
                            "endpoint": endpoint
                        }
                    )
                else:
                    return HTMLResponse(
                        status_code=429,
                        content=f"<html><body><h1>Too many requests</h1><p>Too many concurrent requests for {endpoint}. Please wait a moment.</p></body></html>"
                    )
            active_requests[endpoint] += 1
            counted = True
        
        try:
            # Führe Request mit Timeout aus
            response = await asyncio.wait_for(call_next(request), timeout=150.0)  # 150s für VPN/Netzwerk-Probleme
            
            # Request erfolgreich → reset Circuit Breaker (nur bei erfolgreichen Responses)
            if response.status_code < 400:
                if endpoint in circuit_breaker:
                    circuit_breaker[endpoint] = {'failures': 0, 'last_failure': 0, 'state': 'closed'}
            elif response.status_code >= 500:
                # Nur Server-Fehler (5xx) zählen für Circuit Breaker.
                # 4xx (z.B. 401/403 bei abgelaufener Session) sind Client/Auth-Fehler
                # und dürfen den Endpoint nicht "OPEN" schalten.
                if endpoint not in circuit_breaker:
                    circuit_breaker[endpoint] = {'failures': 0, 'last_failure': 0, 'state': 'closed'}
                circuit_breaker[endpoint]['failures'] += 1
                circuit_breaker[endpoint]['last_failure'] = time.time()
                if circuit_breaker[endpoint]['failures'] >= CIRCUIT_BREAKER_FAILURE_THRESHOLD:
                    circuit_breaker[endpoint]['state'] = 'open'
                    logger.error(f"🚫 Circuit Breaker für {endpoint} → OPEN (zu viele Fehler-Responses)")
            
            return response
        except asyncio.TimeoutError:
            logger.error(f"⏱️ Timeout für {endpoint} nach 150s")
            # Erhöhe Circuit Breaker Failures
            if endpoint not in circuit_breaker:
                circuit_breaker[endpoint] = {'failures': 0, 'last_failure': 0, 'state': 'closed'}
            circuit_breaker[endpoint]['failures'] += 1
            circuit_breaker[endpoint]['last_failure'] = time.time()
            if circuit_breaker[endpoint]['failures'] >= CIRCUIT_BREAKER_FAILURE_THRESHOLD:
                circuit_breaker[endpoint]['state'] = 'open'
                logger.error(f"🚫 Circuit Breaker für {endpoint} → OPEN (zu viele Timeouts)")
            
            return JSONResponse(
                status_code=504,
                content={
                    "success": False,
                    "error": "Request timeout (150s)",
                    "endpoint": endpoint
                }
            )
        except Exception as e:
            logger.error(f"❌ Fehler in Middleware für {endpoint}: {e}", exc_info=True)
            # Erhöhe Circuit Breaker Failures
            if endpoint not in circuit_breaker:
                circuit_breaker[endpoint] = {'failures': 0, 'last_failure': 0, 'state': 'closed'}
            circuit_breaker[endpoint]['failures'] += 1
            circuit_breaker[endpoint]['last_failure'] = time.time()
            if circuit_breaker[endpoint]['failures'] >= CIRCUIT_BREAKER_FAILURE_THRESHOLD:
                circuit_breaker[endpoint]['state'] = 'open'
                logger.error(f"🚫 Circuit Breaker für {endpoint} → OPEN (zu viele Fehler)")
            
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": str(e),
                    "endpoint": endpoint
                }
            )
        finally:
            if counted:
                active_requests[endpoint] = max(0, active_requests[endpoint] - 1)
            duration = time.time() - start_time
            if duration > 5.0:  # Logge langsame Requests
                logger.warning(f"⚠️ Langsamer Request: {endpoint} dauerte {duration:.2f}s")
    except Exception as e:
        logger.error(f"❌ Kritischer Fehler in Middleware: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Internal server error",
                "endpoint": endpoint
            }
        )


@app.websocket("/ws/positions/{account}/{symbol}")
async def websocket_positions(websocket: WebSocket, account: str, symbol: str):
    """Stream position updates per account and symbol (main=long, sub=short). Bei ?profile=bot_1/bot_2: Long_bot_1/Short_bot_1."""
    if account not in WS_ACCOUNT_TO_BOT_TYPE:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    symbol = symbol.strip().upper()
    profile = (websocket.query_params.get("profile") or "").strip()
    if profile not in ("main", "bot_1", "bot_2"):
        profile = None

    try:
        initial_payload = _build_ws_payload_for_account(symbol, account, profile=profile)
        await websocket.send_json({
            "type": "initial_data",
            "account": account,
            "symbol": symbol,
            "data": initial_payload
        })

        while True:
            payload = _build_ws_payload_for_account(symbol, account, profile=profile)
            await websocket.send_json({
                "type": "position_update",
                "account": account,
                "symbol": symbol,
                "data": payload
            })
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected ({account}:{symbol})")
    except Exception as e:
        logger.warning(f"[WS] Error in websocket ({account}:{symbol}): {e}")


# Helper function for sudo commands (always uses password from config.yaml)
def run_sudo_command(command: list, timeout: int = 10) -> subprocess.CompletedProcess:
    """
    Führt einen sudo-Befehl aus. Verwendet immer das Passwort aus config.yaml.
    Keine Zeitverschwendung durch Versuche ohne Passwort.
    
    :param command: Liste mit Befehl und Argumenten (z.B. ['sudo', 'systemctl', 'start', 'service'])
    :param timeout: Timeout in Sekunden
    :return: subprocess.CompletedProcess
    """
    sudo_password = get_sudo_password()
    
    # Füge -S hinzu für Passwort-Eingabe, falls nicht bereits vorhanden
    if command[0] == 'sudo' and '-S' not in command:
        password_cmd = ['sudo', '-S'] + command[1:]
    else:
        password_cmd = command
    
    return subprocess.run(
        password_cmd,
        input=f'{sudo_password}\n',
        capture_output=True,
        text=True,
        timeout=timeout
    )


def systemctl_stop(service_name: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """
    Stoppt einen systemd-Service. Versucht zuerst ohne sudo (User-Services / NOPASSWD),
    bei Fehler mit sudo und Passwort aus config.
    """
    try:
        # Erster Versuch ohne sudo: kurzer Timeout (3s), damit wir schnell zu sudo wechseln
        r = subprocess.run(
            ['systemctl', 'stop', service_name],
            capture_output=True,
            text=True,
            timeout=min(3, timeout)
        )
        if r.returncode == 0:
            return r
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass
    return run_sudo_command(['sudo', 'systemctl', 'stop', service_name], timeout=timeout)


async def systemctl_stop_async(service_name: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Wie systemctl_stop, aber in einem Thread – blockiert den Event-Loop nicht."""
    return await asyncio.to_thread(systemctl_stop, service_name, timeout)


async def _stop_bot_for_restart(symbol: str, bot_type: str) -> bool:
    """
    Stoppt einen Bot für den Restart. Schneller Pfad für skriptgestartete Bots (PID-Datei).
    Returns True wenn Bot gestoppt oder nicht aktiv, False wenn Stop fehlgeschlagen.
    """
    # Fast path: Skriptgestartete Bots – PID-Datei vorhanden → direkt SIGTERM/SIGKILL
    pid, pid_file = await asyncio.to_thread(get_bot_pid_from_run_dir, symbol, bot_type)
    if pid:
        logger.info(f"[RESTART-STOP] Fast path: stoppe {bot_type}-Bot {symbol} via PID {pid}")
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        for _ in range(4):
            try:
                os.kill(pid, 0)
                await asyncio.sleep(0.08)
            except ProcessLookupError:
                break
            except Exception:
                break
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        try:
            if pid_file:
                pid_file.unlink(missing_ok=True)
        except Exception:
            pass
        if await asyncio.to_thread(is_bot_running, symbol, bot_type):
            try:
                await asyncio.to_thread(
                    lambda: subprocess.run(
                        ["pkill", "-f", f"{bot_type}_bot.py {symbol}"],
                        capture_output=True, text=True, timeout=5,
                    )
                )
            except Exception:
                pass
            await asyncio.sleep(0.15)
        return not await asyncio.to_thread(is_bot_running, symbol, bot_type)

    # Fallback: systemd oder andere Quellen
    if not await asyncio.to_thread(is_bot_running, symbol, bot_type):
        return True
    service_name = f"hedgebot-{bot_type}@{symbol}"
    stop_result = await systemctl_stop_async(service_name, timeout=5)
    if stop_result.returncode != 0:
        logger.warning("[RESTART-STOP] systemctl stop failed for %s, returncode=%s, trying PID fallback", symbol, stop_result.returncode)
    await asyncio.sleep(0.2)
    if await asyncio.to_thread(is_bot_running, symbol, bot_type):
        logger.info(f"[RESTART-STOP] Bot noch aktiv nach systemctl, PID-Fallback für {symbol}")
        safe_symbol = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in str(symbol))
        run_dir = project_root / "data" / "run"
        for pid_name in (f"{bot_type}_bot_{safe_symbol}.pid", f"{bot_type}_bot_{safe_symbol}_bot_1.pid", f"{bot_type}_bot_{safe_symbol}_bot_2.pid"):
            pid_path = run_dir / pid_name
            if pid_path.exists():
                try:
                    pid_raw = pid_path.read_text(encoding="utf-8").strip()
                    if pid_raw.isdigit():
                        pid = int(pid_raw)
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except (ProcessLookupError, PermissionError):
                            pass
                        for _ in range(4):
                            try:
                                os.kill(pid, 0)
                                await asyncio.sleep(0.08)
                            except ProcessLookupError:
                                break
                            except Exception:
                                break
                        else:
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except Exception:
                                pass
                except Exception:
                    pass
        try:
            pid_json = project_root / "data" / "logs" / "local_bots_pids.json"
            if pid_json.exists():
                with open(pid_json, "r", encoding="utf-8") as f:
                    pids_dict = json.load(f)
                bot_key = f"{bot_type}_{symbol}"
                if bot_key in pids_dict and str(pids_dict[bot_key]).isdigit():
                    pid = int(pids_dict[bot_key])
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
                    for _ in range(4):
                        try:
                            os.kill(pid, 0)
                            await asyncio.sleep(0.08)
                        except ProcessLookupError:
                            break
                        except Exception:
                            break
                    else:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except Exception:
                            pass
        except Exception:
            pass
        try:
            await asyncio.to_thread(
                lambda: subprocess.run(
                    ["pkill", "-f", f"{bot_type}_bot.py {symbol}"],
                    capture_output=True, text=True, timeout=5,
                )
            )
        except Exception:
            pass
        await asyncio.sleep(0.2)
    return not await asyncio.to_thread(is_bot_running, symbol, bot_type)


def systemctl_start(service_name: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """
    Startet einen systemd-Service. Versucht zuerst ohne sudo, bei Fehler mit sudo.
    """
    try:
        r = subprocess.run(
            ['systemctl', 'start', service_name],
            capture_output=True,
            text=True,
            timeout=min(3, timeout)
        )
        if r.returncode == 0:
            return r
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass
    return run_sudo_command(['sudo', 'systemctl', 'start', service_name], timeout=timeout)


async def systemctl_start_async(service_name: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Wie systemctl_start, aber in einem Thread – blockiert den Event-Loop nicht."""
    return await asyncio.to_thread(systemctl_start, service_name, timeout)


def _normalize_systemctl_error(
    stderr: str, stdout: str, returncode: int | None = None
) -> str:
    """Ersetzt sudo-Passwort-Prompt durch verständlichen Hinweis; bei leerer Ausgabe Returncode anzeigen."""
    err = (stderr or stdout or "").strip()
    if "[sudo] password for" in err or "sudo" in err.lower() and "password" in err.lower():
        return (
            "systemctl benötigt Rechte. Entweder: "
            "In config/config.yaml unter system.sudo_password dein Passwort eintragen, "
            "oder sudo ohne Passwort für systemctl einrichten (z.B. sudo visudo)."
        )
    if err:
        return err
    if returncode is not None:
        return (
            f"systemctl stop fehlgeschlagen (Exit-Code {returncode}, keine Ausgabe). "
            "Mögliche Ursachen: Service-Name unbekannt (Bot nicht als systemd-Service?), "
            "oder sudo-Passwort in config/config.yaml prüfen."
        )
    return "Unbekannter Fehler"


def render_template(template_name: str, context: dict) -> str:
    template = jinja_env.get_template(template_name)
    return template.render(**context)

# Static files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Simple session storage (in production, use proper session management)
sessions = {}

# Pydantic models for request bodies
class BotActionRequest(BaseModel):
    bot_type: str = "long"
    profile: Optional[str] = None  # main|bot_1|bot_2 für profil-spezifische PID-Dateien


def get_current_user(request: Request) -> dict | None:
    """Get current user from session"""
    session_id = request.cookies.get("session_id")
    if session_id and session_id in sessions:
        return sessions[session_id]
    return None


def require_auth(request: Request):
    """Dependency to require authentication"""
    user = get_current_user(request)
    if not user:
        logger.warning(f"[AUTH] Authentication failed for {request.url}")
        raise HTTPException(status_code=401, detail="Not authenticated")
    logger.debug(f"[AUTH] User authenticated: {user.get('username', 'unknown')}")
    return user


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Redirect to login if not authenticated, else dashboard"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return RedirectResponse(url="/dashboard", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page"""
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return HTMLResponse(render_template("login.html", {"request": request}))


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """Handle login"""
    user = authenticate_user(username, password)
    if not user:
        return HTMLResponse(render_template(
            "login.html",
            {"request": request, "error": "Invalid username or password"}
        ))
    
    # Create session
    import secrets
    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = user
    
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(key="session_id", value=session_id, httponly=True, max_age=86400)  # 24 Stunden (statt 30 min)
    return response


@app.get("/logout")
async def logout(request: Request):
    """Handle logout"""
    session_id = request.cookies.get("session_id")
    if session_id and session_id in sessions:
        del sessions[session_id]
    
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session_id")
    return response


@app.get("/api/health")
async def api_health():
    """
    Health-Check-Endpoint für Watchdog/Monitoring.
    Gibt sofort eine Antwort zurück, um zu prüfen, ob das Dashboard noch läuft.
    """
    try:
        now_ts = time.time()
        # Prüfe ob kritische Komponenten funktionieren
        health_status = {
            "status": "healthy",
            "timestamp": now_ts,
            "uptime": now_ts - app_start_time if 'app_start_time' in globals() else 0,
            "components": {
                "api": "ok",
                "cache": "ok",
                "circuit_breaker": "ok"
            },
            "circuit_breaker_debug": {
                "open_count": 0,
                "open_endpoints": []
            },
        }
        
        # Prüfe Circuit Breaker Status
        open_circuits = sum(1 for cb in circuit_breaker.values() if cb.get('state') == 'open')
        open_endpoints = []
        for endpoint, cb in circuit_breaker.items():
            if cb.get("state") == "open":
                retry_after = max(
                    0.0,
                    float(CIRCUIT_BREAKER_RESET_TIMEOUT) - (now_ts - float(cb.get("last_failure", 0.0))),
                )
                open_endpoints.append({
                    "endpoint": endpoint,
                    "failures": int(cb.get("failures", 0)),
                    "retry_after_seconds": round(retry_after, 1),
                })

        if open_circuits > 0:
            health_status["components"]["circuit_breaker"] = f"warning ({open_circuits} open)"
        health_status["circuit_breaker_debug"]["open_count"] = open_circuits
        health_status["circuit_breaker_debug"]["open_endpoints"] = open_endpoints
        
        return JSONResponse(content=health_status)
    except Exception as e:
        logger.error(f"Health check error: {e}", exc_info=True)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "error": str(e),
                "timestamp": time.time()
            }
        )


@app.post("/api/heatmap/upload")
async def upload_heatmap_screenshot(
    request: Request,
    user: dict = Depends(require_auth),
    file: UploadFile = File(...)
):
    """
    Nimmt einen Screenshot im Format
    liquidation-heatmap-binance-<symbol>-<YYYY-MM-DD-HH_MM_SS>.png
    entgegen und speichert ihn im Projekt unter
    scripts/update_chart_prices/.
    """
    filename = file.filename or ""
    pattern = r"^liquidation-heatmap-binance-([a-z0-9]+)-(\d{4}-\d{2}-\d{2}-\d{2}_\d{2}_\d{2})\.png$"

    import re

    match = re.match(pattern, filename)
    if not match:
        raise HTTPException(
            status_code=400,
            detail="Ungültiger Dateiname. Erwartetes Format: "
                   "liquidation-heatmap-binance-<symbol>-<YYYY-MM-DD-HH_MM_SS>.png"
        )

    symbol = match.group(1)
    timestamp = match.group(2)

    project_root = Path(__file__).resolve().parent.parent
    target_dir = project_root / "scripts" / "update_chart_prices"
    target_dir.mkdir(parents=True, exist_ok=True)

    # Immer nur eine PNG im Ordner behalten und diese als charts.png verwenden
    target_path = target_dir / "charts.png"

    contents = await file.read()
    try:
        # Alle bestehenden PNGs im Ordner löschen, damit nichts „liegen bleibt“
        for old_file in target_dir.glob("*.png"):
            try:
                old_file.unlink()
            except Exception as cleanup_exc:
                logger.warning(f"[HEATMAP-UPLOAD] Konnte alte Datei nicht löschen: {old_file} – {cleanup_exc}")

        # Neue Datei als charts.png speichern
        with open(target_path, "wb") as f:
            f.write(contents)
    except Exception as exc:
        logger.error(f"[HEATMAP-UPLOAD] Fehler beim Speichern der Datei {target_path}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Fehler beim Speichern der Datei")

    logger.info(
        f"[HEATMAP-UPLOAD] Datei empfangen und gespeichert: {filename} → {target_path} | "
        f"Symbol={symbol.upper()}, timestamp_utc={timestamp}"
    )

    return {
        "success": True,
        "filename": filename,
        "symbol": symbol.upper(),
        "timestamp_utc": timestamp,
        "path": str(target_path),
    }


@app.get("/api/logs/changed", response_class=JSONResponse)
async def check_logs_changed(request: Request, user: dict = Depends(require_auth), bot_type: str = Query(None)):
    """
    API endpoint to check if log files have changed.
    Returns list of symbols whose logs have changed since last check.
    """
    from utils.bot_monitor import get_all_bots
    
    changed_symbols = []
    all_bots = get_all_bots(bot_type=bot_type)
    
    for bot in all_bots:
        bot_symbol = bot["symbol"]
        bot_type_for_bot = bot.get("bot_type", "long")
        # Use symbol+bot_type as key for log file mtimes
        log_key = f"{bot_symbol}_{bot_type_for_bot}"
        last_mtime = log_file_mtimes.get(log_key)
        log_changed, current_mtime = has_log_file_changed(bot_symbol, last_mtime, bot_type=bot_type_for_bot)
        
        if log_changed:
            log_file_mtimes[log_key] = current_mtime
            changed_symbols.append({"symbol": bot_symbol, "bot_type": bot_type_for_bot})
    
    return {"changed": changed_symbols, "timestamp": time.time()}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: dict = Depends(require_auth)):
    """Dashboard page – uses hedge positions view as main dashboard."""
    symbol_param = request.query_params.get("symbol", "")
    logger.info(f"[Dashboard] GET /dashboard - URL={request.url}, query symbol={symbol_param!r}")
    available_profiles = _available_profiles()
    available_profile_labels = _available_profile_labels()
    long_bots = _get_dashboard_long_bots()
    return HTMLResponse(render_template(
        "hedge_positions.html",
        {
            "request": request,
            "user": user,
            "available_profiles": available_profiles,
            "available_profile_labels": available_profile_labels,
            "available_profiles_json": json.dumps(available_profiles),
            "available_profile_labels_json": json.dumps(available_profile_labels),
            "long_bots": long_bots,
            "long_bots_json": json.dumps(long_bots),
        }
    ))


@app.get("/position-calculator", response_class=HTMLResponse)
async def position_calculator(request: Request, user: dict = Depends(require_auth)):
    """Position Calculator page"""
    # Load config values from first available bot or use defaults
    default_config = get_default_config()
    short_reentry_step_percentage = default_config.get('short_reentry_step_percentage', 0.3)
    target_long_notional = default_config.get('initial_long_usdt', 500)
    
    # Try to get config from first available bot
    all_bots = get_all_bots()
    if all_bots and len(all_bots) > 0:
        first_bot = all_bots[0]
        first_bot_type = first_bot.get("bot_type", "long")
        first_bot_config = load_config(bot_type=first_bot_type)
        if first_bot_config and 'short_reentry_step_percentage' in first_bot_config:
            short_reentry_step_percentage = first_bot_config.get('short_reentry_step_percentage', 0.3)
        if first_bot_config and 'initial_long_usdt' in first_bot_config:
            target_long_notional = first_bot_config.get('initial_long_usdt', 500)
    
    # Get all available bots for symbol selection
    all_bots_list = get_all_bots()
    
    return HTMLResponse(render_template(
        "position_calculator.html",
        {
            "request": request,
            "user": user,
            "short_reentry_step_percentage": short_reentry_step_percentage,
            "target_long_notional": target_long_notional,
            "all_bots": all_bots_list
        }
    ))


@app.get("/dual-account-hedge")
async def redirect_dual_account_hedge(request: Request, user: dict = Depends(require_auth)):
    """Redirect alte URL auf Profit-Verlauf"""
    return RedirectResponse(url="/profit-verlauf", status_code=302)


@app.get("/profit-verlauf", response_class=HTMLResponse)
async def profit_verlauf(request: Request, user: dict = Depends(require_auth)):
    """Profit-Verlauf page – Burn- und Exit-Profits (vormals 2-Account Hedge)"""
    # Load default config values
    default_config = get_default_config()
    default_long_notional = default_config.get('initial_long_usdt', 500)
    default_short_notional = default_config.get('initial_short_usdt', 250)
    
    # Try to get config from existing configs
    try:
        long_config = load_config(bot_type="long")
        if long_config:
            if 'initial_long_usdt' in long_config:
                default_long_notional = long_config.get('initial_long_usdt', 500)
    except:
        pass
    
    try:
        short_config = load_config(bot_type="short")
        if short_config:
            if 'initial_short_usdt' in short_config:
                default_short_notional = short_config.get('initial_short_usdt', 250)
    except:
        pass
    
    return HTMLResponse(render_template(
        "dual_account_hedge.html",
        {
            "request": request,
            "user": user,
            "default_long_notional": default_long_notional,
            "default_short_notional": default_short_notional,
            "bot_profiles": _serializable_bot_profiles(),
            "dashboard_accounts": get_dashboard_accounts(),
            "closed_pnl_accounts": get_closed_pnl_accounts(),
        }
    ))


@app.get("/profit-verlauf_2", response_class=HTMLResponse)
async def profit_verlauf_2(
    request: Request,
    profile: str = Query("bot_1", description="Bot profile (e.g. bot_1)"),
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(require_auth),
):
    trades, warnings = _load_trade_blocks_for_profile(profile, limit)
    normalized = [_normalize_trade_record(evt) for evt in trades]
    normalized.sort(key=_score_trade_record_end_time, reverse=True)
    summary = _summarize_trade_blocks(normalized)
    trade_rows = [_build_profit_trade_summary(record) for record in normalized]
    return HTMLResponse(render_template(
        "profit_verlauf_2.html",
        {
            "request": request,
            "user": user,
            "profile": profile,
            "summary": summary,
            "trades": trade_rows,
            "warnings": warnings,
            "trade_limit": limit,
        },
    ))


@app.get("/multi-zyklus-chart", response_class=HTMLResponse)
async def multi_zyklus_chart(request: Request, user: dict = Depends(require_auth)):
    """Multi Zyklus aus Chart – Chart hochladen, Preise lesen, Configs generieren."""
    return HTMLResponse(render_template(
        "multi_zyklus_chart.html",
        {"request": request, "user": user}
    ))


@app.get("/price-alert", response_class=HTMLResponse)
async def price_alert_page(request: Request, user: dict = Depends(require_auth)):
    """Price Alert – Benachrichtigung wenn Coin einen Zielpreis erreicht (Above/Below)."""
    return HTMLResponse(render_template(
        "price_alert.html",
        {"request": request, "user": user}
    ))


_PRICE_ALERT_STATE_FILE = project_root / "data" / "state" / "price_alert.json"


def _fetch_bybit_ticker_price(symbol: str) -> float | None:
    """Holt letzten Preis von Bybit Market Tickers (öffentliche API, keine Auth)."""
    try:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None
        url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}"
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data.get("result") or {}
        items = result.get("list") or []
        if not items:
            return None
        last = items[0].get("lastPrice")
        return float(last) if last is not None else None
    except Exception:
        return None


def _load_price_alert_state() -> dict:
    """Lädt aktive Price-Alerts aus State-Datei."""
    if not _PRICE_ALERT_STATE_FILE.exists():
        return {}
    try:
        with open(_PRICE_ALERT_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Migration: alte Keys (nur Symbol) -> neuer Key (symbol_target_trigger)
        result = {}
        for k, v in data.items():
            if isinstance(v, dict) and "target_price" in v and "trigger" in v:
                sym = v.get("symbol") or k
                tp = v.get("target_price")
                tr = v.get("trigger", "above")
                new_key = f"{sym}_{tp:.6f}_{tr}" if isinstance(tp, (int, float)) else f"{sym}_{tp}_{tr}"
                result[new_key] = {**v, "symbol": str(sym).strip().upper()}
            else:
                result[k] = v
        return result
    except Exception:
        return {}


def _price_alert_key(symbol: str, target_price: float, trigger: str) -> str:
    """Eindeutiger Key für mehrere Alerts pro Symbol."""
    return f"{symbol}_{target_price:.6f}_{trigger}"


@app.get("/api/price-alert/current-price")
async def api_price_alert_current_price(
    symbol: str = Query(..., description="Trading-Symbol (z.B. TONUSDT)"),
    user: dict = Depends(require_auth),
):
    """Aktueller Preis von Bybit (Market Tickers, linear/USDT)."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {"success": False, "error": "symbol fehlt"}
    price = await asyncio.to_thread(_fetch_bybit_ticker_price, sym)
    if price is None:
        return {"success": False, "error": "Preis nicht abrufbar"}
    return {"success": True, "price": price, "symbol": sym}


@app.post("/api/price-alert/start")
async def api_price_alert_start(
    payload: dict = Body(...),
    user: dict = Depends(require_auth),
):
    """Startet Price-Alert-Script im Hintergrund (pollt Bybit, benachrichtigt ntfy)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    symbol = str((payload.get("symbol") or "").strip().upper())
    try:
        target_price = float(payload.get("target_price"))
    except (TypeError, ValueError):
        return {"success": False, "error": "target_price fehlt oder ungültig"}
    trigger = str((payload.get("trigger") or "").strip().lower() or "above")
    if trigger not in ("above", "below"):
        return {"success": False, "error": "trigger muss 'above' oder 'below' sein"}
    if not symbol:
        return {"success": False, "error": "symbol fehlt"}
    if target_price <= 0:
        return {"success": False, "error": "target_price muss > 0 sein"}
    script_path = project_root / "scripts" / "price_alert.py"
    if not script_path.exists():
        return {"success": False, "error": f"Script nicht gefunden: {script_path}"}
    state = _load_price_alert_state()
    alert_key = _price_alert_key(symbol, target_price, trigger)
    if alert_key in state:
        return {"success": False, "error": f"Alert für {symbol} {trigger} {target_price} bereits aktiv."}
    try:
        proc = subprocess.Popen(
            [sys.executable, str(script_path), "--symbol", symbol, "--target-price", str(target_price), "--trigger", trigger],
            cwd=str(project_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONPATH": str(project_root)},
        )
        state[alert_key] = {
            "symbol": symbol,
            "pid": proc.pid,
            "target_price": target_price,
            "trigger": trigger,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        }
        _PRICE_ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_PRICE_ALERT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        return {"success": True, "message": f"Alert aktiv: {symbol} {trigger} {target_price}.", "symbol": symbol}
    except Exception as e:
        logger.error(f"Fehler beim Starten von price_alert: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.get("/api/price-alert/status")
async def api_price_alert_status(user: dict = Depends(require_auth)):
    """Liefert aktive Price-Alerts."""
    data = _load_price_alert_state()
    return {"success": True, "alerts": data}


@app.post("/api/price-alert/stop")
async def api_price_alert_stop(
    payload: dict = Body(...),
    user: dict = Depends(require_auth),
):
    """Beendet einen bestimmten Price-Alert (Symbol + Preis + Trigger)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    symbol = str((payload.get("symbol") or "").strip().upper())
    try:
        target_price = float(payload.get("target_price")) if payload.get("target_price") not in (None, "") else None
    except (TypeError, ValueError):
        target_price = None
    trigger = str((payload.get("trigger") or "").strip().lower() or "above")
    if not symbol:
        return {"success": False, "error": "symbol fehlt"}
    data = _load_price_alert_state()
    alert_key = _price_alert_key(symbol, target_price, trigger) if target_price is not None else None
    if alert_key and alert_key in data:
        entry = data[alert_key]
    else:
        # Fallback: erstes Match für Symbol (z.B. bei nur einem Alert)
        entry = None
        for k, v in data.items():
            if v.get("symbol") == symbol:
                entry = v
                alert_key = k
                break
    if not entry or not alert_key:
        return {"success": True, "message": f"Kein aktiver Alert für {symbol}."}
    pid = entry.get("pid")
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError, ValueError) as e:
        logger.warning(f"Stop Price-Alert: Prozess {pid} nicht erreichbar: {e}")
    data.pop(alert_key, None)
    try:
        _PRICE_ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if data:
            with open(_PRICE_ALERT_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        elif _PRICE_ALERT_STATE_FILE.exists():
            _PRICE_ALERT_STATE_FILE.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"State-Datei nach Stop nicht geschrieben: {e}")
    return {"success": True, "message": f"Alert für {symbol} beendet."}


@app.post("/api/multi-zyklus/upload")
async def api_multi_zyklus_upload(
    user: dict = Depends(require_auth),
    file: UploadFile = File(...),
):
    """Chart-Screenshot hochladen, Preise via OCR auslesen."""
    if not file.filename or not file.filename.lower().endswith((".png", ".jpg", ".jpeg")):
        raise HTTPException(status_code=400, detail="Bitte PNG oder JPEG hochladen.")
    charts_dir = project_root / "scripts" / "update_chart_prices"
    charts_dir.mkdir(parents=True, exist_ok=True)
    temp_path = charts_dir / "multi_zyklus_upload.png"
    try:
        contents = await file.read()
        with open(temp_path, "wb") as f:
            f.write(contents)
    except Exception as e:
        logger.error(f"[multi-zyklus] Upload speichern: {e}")
        raise HTTPException(status_code=500, detail="Fehler beim Speichern der Datei")
    try:
        from scripts.update_chart_prices.read_chart_prices import read_prices_from_image, parse_lines_to_entry_levels
        result = read_prices_from_image(temp_path)
        parsed = parse_lines_to_entry_levels(result=result)
        coin = (result.get("coin") or "UNKNOWN").strip().upper()
        if coin and coin != "UNKNOWN" and not coin.endswith("USDT"):
            coin = coin + "USDT"
        return {
            "success": True,
            "coin": coin,
            "entry": parsed.get("entry"),
            "main_sub_levels": result.get("main_sub_levels", []),
            "long_levels": parsed.get("long_levels", []),
            "short_levels": parsed.get("short_levels", []),
            "raw_lines": result.get("lines", []),
        }
    except Exception as e:
        logger.error(f"[multi-zyklus] read_chart_prices: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/multi-zyklus/generate")
async def api_multi_zyklus_generate(
    user: dict = Depends(require_auth),
    data: dict = Body(...),
):
    """Configs aus Main/Sub-Level und 4 Bot-Levels generieren."""
    symbol = (data.get("symbol") or "").strip().upper()
    if symbol and not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    size = data.get("size")
    main_level = data.get("main_level")
    sub_level = data.get("sub_level")
    long_bot_1 = data.get("long_bot_1")
    long_bot_2 = data.get("long_bot_2")
    short_bot_1 = data.get("short_bot_1")
    short_bot_2 = data.get("short_bot_2")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol fehlt")
    try:
        main_level = float(main_level)
        sub_level = float(sub_level)
        long_bot_1 = float(long_bot_1)
        long_bot_2 = float(long_bot_2) if long_bot_2 not in (None, "") else (long_bot_1 - 0.01)  # Fallback wenn OCR Long Bot 2 verfehlt
        short_bot_1 = float(short_bot_1)
        short_bot_2 = float(short_bot_2) if short_bot_2 not in (None, "") else (short_bot_1 + 0.01)  # Fallback
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Alle Level müssen Zahlen sein (Main, Sub, Long 1, Short 1)")
    if main_level <= 0 or sub_level <= 0:
        raise HTTPException(status_code=400, detail="Main- und Sub-Level müssen positiv sein")
    try:
        initial_usdt = 15.0
        if size is not None and str(size).strip():
            try:
                initial_usdt = float(size)
                if initial_usdt <= 0:
                    initial_usdt = 15.0
            except (TypeError, ValueError):
                pass
    except Exception:
        initial_usdt = 15.0
    try:
        from scripts.generate_cycles_from_chart import generate_bot_configs
        main_cfg, lb1, lb2, sub_cfg, sb1, sb2 = generate_bot_configs(
            symbol, main_level, sub_level,
            long_bot_1, long_bot_2, short_bot_1, short_bot_2,
            initial_usdt=initial_usdt,
        )
        # Configs in config/, config/bot_1, config/bot_2 speichern
        cfg_root = project_root / "config"
        bot_1_dir = cfg_root / "bot_1"
        bot_2_dir = cfg_root / "bot_2"
        bot_1_dir.mkdir(parents=True, exist_ok=True)
        bot_2_dir.mkdir(parents=True, exist_ok=True)
        saved_paths = []
        save_error = None
        files_to_save = [
            (main_cfg, cfg_root / f"long_config_{symbol}.yaml", "long", None),
            (sub_cfg, cfg_root / f"short_config_{symbol}.yaml", "short", None),
            (lb1, bot_1_dir / f"long_config_{symbol}.yaml", "long", "bot_1"),
            (lb2, bot_2_dir / f"long_config_{symbol}.yaml", "long", "bot_2"),
            (sb1, bot_1_dir / f"short_config_{symbol}.yaml", "short", "bot_1"),
            (sb2, bot_2_dir / f"short_config_{symbol}.yaml", "short", "bot_2"),
        ]
        try:
            for cfg, path, bt, prof in files_to_save:
                header = get_config_header_comment(prof, bt, path.name)
                yaml_str = header + format_config_with_blocks(cfg, bot_type=bt)
                path.write_text(yaml_str, encoding="utf-8")
                saved_paths.append(str(path.resolve()))
            logger.info(f"[multi-zyklus] Configs gespeichert: {saved_paths}")
        except Exception as save_err:
            save_error = str(save_err)
            logger.error(f"[multi-zyklus] Speichern fehlgeschlagen: {save_error}", exc_info=True)
        fn_long = f"long_config_{symbol}.yaml"
        fn_short = f"short_config_{symbol}.yaml"
        return {
            "success": True,
            "main_yaml": get_config_header_comment(None, "long", fn_long) + format_config_with_blocks(main_cfg, bot_type="long"),
            "sub_yaml": get_config_header_comment(None, "short", fn_short) + format_config_with_blocks(sub_cfg, bot_type="short"),
            "long_bot_1_yaml": get_config_header_comment("bot_1", "long", fn_long) + format_config_with_blocks(lb1, bot_type="long"),
            "long_bot_2_yaml": get_config_header_comment("bot_2", "long", fn_long) + format_config_with_blocks(lb2, bot_type="long"),
            "short_bot_1_yaml": get_config_header_comment("bot_1", "short", fn_short) + format_config_with_blocks(sb1, bot_type="short"),
            "short_bot_2_yaml": get_config_header_comment("bot_2", "short", fn_short) + format_config_with_blocks(sb2, bot_type="short"),
            "saved_paths": saved_paths,
            "save_error": save_error,
            "project_root": str(project_root.resolve()),
        }
    except Exception as e:
        logger.error(f"[multi-zyklus] generate: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _get_profile_labels(profile: str) -> dict:
    """Labels für Main/Sub-Spalten je Profil (Live-Charts Kachel-Ansicht)."""
    profile = _normalize_dashboard_profile(profile, fallback_to_main=True)
    if is_bot_profile(profile):
        account_name = profile_to_account_name(profile) or "Long Bot"
        bot_number = profile.split("_", 1)[1]
        return {"main_label": account_name, "sub_label": f"Short_bot_{bot_number}"}
    return {"main_label": "Main (Long)", "sub_label": "Sub (Short)"}


def _resolve_account_to_profile_and_type(acc: str) -> tuple[str, str, bool]:
    """
    acc: main|sub|Long_bot_1|Short_bot_1|Long_bot_2|Short_bot_2|Long_bot_3...
    Returns: (profile, bot_type, single_account)
    single_account=True: nur ein Account (z.B. Long_bot_1), kein Main+Sub-Paar
    """
    resolved = resolve_account(acc)
    if not resolved:
        return "main", "long", True
    if resolved["account"] == "sub":
        return "main", "short", True
    if resolved["account"] == "main":
        return "main", "long", True
    profile = resolved["profile"]
    side = resolved["side"]
    bot_type = "short" if side == "short" else "long"
    return profile, bot_type, True


def _get_current_live_charts_accounts() -> tuple[str, ...]:
    return tuple(get_live_charts_accounts())


def _normalize_live_charts_account(account: str | None) -> tuple[str, tuple[str, ...]]:
    accounts = _get_current_live_charts_accounts()
    acc = (account or "main").strip()
    if acc.lower() in ("main", "sub"):
        acc = acc.lower()
    if acc not in accounts:
        acc = "main"
    return acc, accounts


def _normalize_symbol_value(value: Any) -> str | None:
    if not value:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None
    return text.upper()


def _load_json_file(path: Path) -> dict | None:
    if not path or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _symbol_from_active_bot_symbols(bot_name: str) -> tuple[str | None, str]:
    data = _load_json_file(LIVE_BOT_LOGS_ROOT / "state" / "active_bot_symbols.json")
    if not data:
        return None, "active_bot_symbols_json"
    entry = data.get(bot_name) or data.get(bot_name.lower()) or data.get(bot_name.upper())
    if isinstance(entry, dict):
        return _normalize_symbol_value(entry.get("symbol")), "active_bot_symbols_json"
    return None, "active_bot_symbols_json"


def _symbol_from_run_status(bot_name: str) -> tuple[str | None, str]:
    data = _load_json_file(LIVE_BOT_LOGS_ROOT / bot_name / "run" / "status.json")
    if data:
        return _normalize_symbol_value(data.get("symbol")), "run_status_json"
    return None, "run_status_json"


def _symbol_from_state_file(account: str) -> tuple[str | None, str]:
    state_file = _state_file_for_account(account)
    if not state_file:
        return None, "fixed_cycle_state_json"
    data = _load_json_file(state_file)
    if data:
        if sym := _normalize_symbol_value(data.get("symbol")):
            return sym, "fixed_cycle_state_json"
        for key in ("current_symbol", "trading_symbol"):
            if sym := _normalize_symbol_value(data.get(key)):
                return sym, "fixed_cycle_state_json"
        snapshot = data.get("snapshot") or {}
        if isinstance(snapshot, dict):
            if sym := _normalize_symbol_value(snapshot.get("symbol")):
                return sym, "fixed_cycle_state_json"
    return None, "fixed_cycle_state_json"


def _symbol_from_snapshot(account: str) -> tuple[str | None, str]:
    snapshot_file = _wallet_snapshot_file_for_account(account)
    if not snapshot_file:
        return None, "snapshot_json"
    data = _load_json_file(snapshot_file)
    if data:
        return _normalize_symbol_value(data.get("symbol")), "snapshot_json"
    return None, "snapshot_json"


def _symbol_from_best_coin() -> tuple[str | None, str]:
    data = _load_json_file(project_root / "logs" / "best_coin.json")
    if data:
        return _normalize_symbol_value(data.get("symbol")), "best_coin_json"
    return None, "best_coin_json"


def _fetch_bybit_open_orders(account: str, symbol: str, logger: logging.Logger) -> list[dict[str, Any]] | None:
    api_key, secret_key = _get_account_keys(account)
    if not api_key or not secret_key:
        logger.warning("[LIVE-ORDER-LEVELS] missing credentials", {"account": account})
        return None
    try:
        manager = BybitOrderManager(api_key, secret_key)
        return manager.fetch_open_orders_direct(symbol, timeout=5)
    except Exception as exc:
        logger.warning(
            "[LIVE-ORDER-LEVELS] Bybit fetch failed",
            {"account": account, "symbol": symbol, "error": str(exc)},
        )
        return None


def _get_active_symbol_for_live_charts_account(account: str | None) -> tuple[str | None, str]:
    acc, _ = _normalize_live_charts_account(account)
    if not acc.lower().startswith("long_bot_"):
        return None, "none"
    bot_name = acc.lower()
    for getter in (
        _symbol_from_active_bot_symbols,
        _symbol_from_run_status,
    ):
        symbol, source = getter(bot_name)
        if symbol:
            return symbol, source
    for getter in (
        _symbol_from_state_file,
        _symbol_from_snapshot,
    ):
        symbol, source = getter(acc)
        if symbol:
            return symbol, source
    return _symbol_from_best_coin()


def _load_live_chart_order_levels(account: str | None, symbol: str | None) -> tuple[list[dict[str, Any]], str, str, int]:
    acc, _ = _normalize_live_charts_account(account)
    normalized_bot = acc.lower()
    if not normalized_bot.startswith("long_bot_"):
        return [], normalized_bot, symbol or "", 0
    state_file = _state_file_for_account(acc)
    if not state_file or not state_file.exists():
        logger.info(
            "[LIVE-ORDER-LEVELS] state file missing",
            {"account": acc, "bot_name": normalized_bot, "state_file": str(state_file or "")},
        )
        return [], normalized_bot, symbol or "", 0
    data = _load_json_file(state_file)
    if not data:
        logger.warning(
            "[LIVE-ORDER-LEVELS] state file unreadable",
            {"account": acc, "bot_name": normalized_bot, "state_file": str(state_file)},
        )
        return [], normalized_bot, symbol or "", 0
    active_orders_raw = data.get("active_orders") or data.get("orders") or []
    active_orders = list(active_orders_raw.values()) if isinstance(active_orders_raw, dict) else list(active_orders_raw)

    state_lookup: dict[str, dict[str, Any]] = {}
    for order in active_orders:
        metadata = order.get("metadata") or {}
        for key in (
            str(order.get("client_order_id") or "").strip(),
            str(order.get("exchange_order_id") or "").strip(),
            str(order.get("order_id") or "").strip(),
            str(metadata.get("order_link_id") or "").strip(),
            str(metadata.get("orderLinkId") or "").strip(),
        ):
            if key:
                state_lookup[key] = order

    symbol_upper = (symbol or "").strip().upper()
    if not symbol_upper:
        return [], normalized_bot, symbol or "", 0

    bybit_orders = _fetch_bybit_open_orders(acc, symbol_upper, logger)
    if bybit_orders is None:
        logger.warning("[LIVE-ORDER-LEVELS] Bybit order fetch failed", {"account": acc, "symbol": symbol_upper})
        return [], normalized_bot, symbol or "", 0
    if not bybit_orders:
        return [], normalized_bot, symbol or "", 0

    def _role_text(entry: dict[str, Any]) -> str:
        candidates = [
            entry.get("purpose"),
            entry.get("order_type"),
            entry.get("orderRole"),
            entry.get("order_role"),
            entry.get("stop_order_type"),
            entry.get("client_order_id"),
            entry.get("clientOrderId"),
            entry.get("order_link_id"),
            entry.get("orderLinkId"),
            (entry.get("metadata") or {}).get("order_role"),
            (entry.get("metadata") or {}).get("purpose"),
            (entry.get("metadata") or {}).get("order_type"),
            (entry.get("metadata") or {}).get("client_order_id"),
            (entry.get("metadata") or {}).get("clientOrderId"),
            (entry.get("metadata") or {}).get("order_link_id"),
            (entry.get("metadata") or {}).get("orderLinkId"),
        ]
        return " ".join(filter(None, (str(part) for part in candidates))).lower()

    def _determine_label(role_text: str) -> tuple[str, str, str]:
        level_type = "final_exit"
        if any(keyword in role_text for keyword in ["long_add", "cycle_long_add", "long_reduce", "reduce_long", "long_sl"]):
            level_type = "long_add"
        elif any(keyword in role_text for keyword in ["short_tp", "cycle_short_tp", "short_take_profit", "take_profit_short", "tp_short", "short_reduce"]):
            level_type = "short_tp"
        elif any(keyword in role_text for keyword in ["final_exit", "long_tp_exit", "short_sl_exit", "long_exit", "short_exit", "exit_price"]):
            level_type = "final_exit"
        label_map = {"long_add": "Long Add", "short_tp": "Short TP", "final_exit": "Final Exit"}
        color_map = {"long_add": "#ff3b30", "short_tp": "#00c853", "final_exit": "#ff9800"}
        return level_type, label_map.get(level_type, "Order"), color_map.get(level_type, "#f97316")

    levels: list[dict[str, Any]] = []
    debug_orders: list[dict[str, Any]] = []
    purposes: set[str] = set()
    for order in bybit_orders:
        info = order.get("info") or {}
        side = (info.get("side") or order.get("side") or "").lower()
        order_link = str(info.get("orderLinkId") or info.get("order_link_id") or "").strip()
        entry_key = order_link or str(order.get("id") or order.get("orderId") or "").strip()
        state_entry = state_lookup.get(entry_key)
        current_role_text = _role_text(state_entry or {"metadata": info, "purpose": info.get("purpose"), "order_type": info.get("orderType")})
        level_type, label, color = _determine_label(current_role_text)
        purposes.add(current_role_text)
        price = float(info.get("triggerPrice") or info.get("tpLimitPrice") or order.get("price") or info.get("price") or 0)
        if price <= 0:
            continue
        price_source = "triggerPrice" if info.get("triggerPrice") else "tpLimitPrice" if info.get("tpLimitPrice") else "price"
        levels.append(
            {
                "price": price,
                "price_source": price_source,
                "label": label,
                "side": side,
                "color": color,
                "source": "bybit_open_orders",
                "level_source": "bybit_open_orders",
                "order_status": order.get("status") or info.get("orderStatus") or "",
                "order_id": str(order.get("id") or order.get("orderId") or ""),
                "exchange_order_id": state_entry.get("exchange_order_id") if state_entry else str(order.get("orderId") or ""),
                "client_order_id": state_entry.get("client_order_id") if state_entry else order_link,
                "role_text": current_role_text,
                "verified_on_bybit": True,
            }
        )
        debug_orders.append(
            {
                "client_order_id": levels[-1]["client_order_id"],
                "role_text": current_role_text,
                "selected_price": levels[-1]["price"],
                "price_source": levels[-1]["price_source"],
                "detected_side": levels[-1]["side"],
            }
        )

    logger.debug(
        "[LIVE-ORDER-LEVELS]",
        {
            "account": account,
            "bot_name": normalized_bot,
            "symbol": symbol_upper,
            "state_file": str(state_file),
            "levels_returned": len(levels),
            "purposes_found": list(purposes),
        },
    )
    return levels, normalized_bot, symbol or "", len(levels), debug_orders


@app.get("/api/live-order-levels")
async def api_live_order_levels(
    account: str = Query("main", description="main|sub|Long_bot_N|Short_bot_N"),
    symbol: str = Query("", description="Symbol, z.B. CHZUSDT"),
):
    sym = (symbol or "").strip().upper() or None
    levels, bot_name, resolved_symbol, count, debug_orders = _load_live_chart_order_levels(account, sym)
    return JSONResponse(
        {
            "ok": True,
            "account": account,
            "bot_name": bot_name,
            "symbol": resolved_symbol,
            "levels": levels,
            "count": count,
            "debug_orders": debug_orders,
        }
    )


@app.get("/live-charts", response_class=HTMLResponse)
async def live_charts(
    request: Request,
    user: dict = Depends(require_auth),
    symbol: str = Query("", description="Symbol, z.B. FILUSDT"),
    account: str = Query("main", description="main|sub|Long_bot_N|Short_bot_N"),
):
    """
    Live Charts Page – zeigt einen Lightweight-Charts-Preis-Chart für ein Symbol
    mit Burn-Levels. Account wählbar: main, sub, Long_bot_1, Short_bot_1, Long_bot_2, Short_bot_2.
    """
    acc, chart_accounts = _normalize_live_charts_account(account)

    profile, bot_type, single_account = _resolve_account_to_profile_and_type(acc)

    # Verfügbare Symbole: aus Config-Dateien des Profils
    cfg_dir = project_root / "config" / (profile if profile in ("bot_1", "bot_2") else ".")
    try:
        configs = list(cfg_dir.glob("long_config_*.yaml")) + list(cfg_dir.glob("short_config_*.yaml"))
        active_symbols = sorted({c.stem.replace("long_config_", "").replace("short_config_", "") for c in configs if "USDT" in c.stem})
    except Exception:
        active_symbols = []

    requested_symbol = (symbol or "").strip().upper()
    resolved_symbol, symbol_source = _get_active_symbol_for_live_charts_account(acc)
    if requested_symbol:
        if resolved_symbol and requested_symbol != resolved_symbol:
            sym = resolved_symbol
        else:
            sym = requested_symbol
    else:
        sym = resolved_symbol
    if sym and sym not in active_symbols:
        active_symbols = [sym] + active_symbols
    if not sym and active_symbols:
        sym = active_symbols[0]

    symbol_for_log = sym
    if not symbol_for_log:
        symbol_for_log = requested_symbol or ""

    logger.debug(
        "[LIVE-CHARTS] symbols",
        {
            "requested_account": account,
            "normalized_account": acc,
            "normalized_bot_name": acc.lower(),
            "requested_symbol": requested_symbol,
            "resolved_active_symbol": resolved_symbol,
            "symbol_source": symbol_source,
            "final_selected_symbol": sym,
            "available_symbols": active_symbols,
        },
    )

    burn_levels: list[float] = []
    exit_levels: list[float] = []
    long_entry_price = None
    short_entry_price = None
    short_tp_price = None

    # Burn-Levels + Exit-Levels aus Config laden (Profil: config/bot_1/ oder config/)
    try:
        cfg_name = f"{'long' if bot_type == 'long' else 'short'}_config_{sym}.yaml"
        if profile in ("bot_1", "bot_2"):
            cfg_path = project_root / "config" / profile / cfg_name
            if not cfg_path.exists():
                cfg_path = project_root / "config" / cfg_name
        else:
            cfg_path = project_root / "config" / cfg_name
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            raw_levels = cfg.get("burn_levels") or []
            burn_levels = [float(x) for x in raw_levels if isinstance(x, (int, float, str)) and str(x).strip()]
            # Exit-Levels: feste TP-Linien (TP1/TP2/...) – global im Symbol-Config
            try:
                raw_exit = cfg.get("exit_levels") or []
                exit_levels = []
                for x in raw_exit if isinstance(raw_exit, list) else []:
                    if x is None:
                        continue
                    if isinstance(x, (int, float)):
                        v = float(x)
                    elif isinstance(x, str) and x.strip():
                        v = float(x.strip().replace(",", "."))
                    else:
                        continue
                    if v > 0:
                        exit_levels.append(v)
                # Backward compat: fixed_price ohne exit_levels → fixed_price als TP1 zeichnen
                if not exit_levels:
                    key = "long_tp_fixed_price" if bot_type == "long" else "short_tp_fixed_price"
                    fp = cfg.get(key)
                    try:
                        fpv = float(fp) if fp is not None and str(fp).strip() else None
                    except Exception:
                        fpv = None
                    if fpv and fpv > 0:
                        exit_levels = [fpv]
            except Exception:
                exit_levels = []
            # Alle next_cycles: burn_levels dazu sammeln
            for cycle in (cfg.get("next_cycles") or []):
                if not isinstance(cycle, dict):
                    continue
                raw = cycle.get("burn_levels") or []
                for x in raw:
                    if isinstance(x, (int, float, str)) and str(x).strip():
                        try:
                            burn_levels.append(float(x))
                        except (TypeError, ValueError):
                            pass
            # Sortieren, Duplikate entfernen (einheitliche Reihenfolge im Chart)
            seen: set[float] = set()
            burn_levels = [p for p in sorted(burn_levels) if p not in seen and not seen.add(p)]
    except Exception as e:
        logger.warning(f"[LIVE-CHARTS] Konnte Burn-Levels für {sym} nicht laden: {e}", exc_info=True)
        burn_levels = []
        exit_levels = []

    # Live-Entry-Preise: Einzelner Account (main, sub, Long_bot_1, Short_bot_1, …)
    try:
        live = _get_live_positions_snapshot(acc, sym, profile=None) or {}
        long_live = live.get("long") or {}
        short_live = live.get("short") or {}

        lp = float((long_live or {}).get("entry_price") or 0) or None
        sp = float((short_live or {}).get("entry_price") or 0) or None
        if lp:
            long_entry_price = lp
        if sp:
            short_entry_price = sp

        # Short-TP nur für Short-Bots aus Bot-State (order_config.current_short_tp_price)
        if bot_type == "short":
            try:
                short_state = load_bot_state(sym, bot_type="short")
                order_cfg = short_state.get("order_config") or {}
                stp = order_cfg.get("current_short_tp_price")
                if stp is not None:
                    stp_f = float(stp)
                    if stp_f:
                        short_tp_price = stp_f
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[LIVE-CHARTS] Konnte Entry-Preise für {sym} nicht laden: {e}", exc_info=True)

    # Kachel-Labels: Einzelner Account → nur eine Spalte
    if single_account:
        main_label = acc  # z.B. Long_bot_1, Short_bot_1
        sub_label = ""
    else:
        main_label, sub_label = "Main (Long)", "Sub (Short)"

    if acc.startswith("Long_bot_"):
        threading.Thread(
            target=_maybe_run_dashboard_start_snapshot,
            args=(acc.lower(),),
            daemon=True,
        ).start()

    return HTMLResponse(render_template(
        "live_charts.html",
        {
            "request": request,
            "user": user,
            "symbol": sym,
            "account": acc,
            "profile": profile,
            "single_account": single_account,
            "bot_type": bot_type,
            "available_symbols": active_symbols,
            "burn_levels": burn_levels,
            "exit_levels": exit_levels,
            "chart_accounts": chart_accounts,
            "bot_profiles": _serializable_bot_profiles(),
            "long_entry_price": long_entry_price,
            "short_entry_price": short_entry_price,
            "short_tp_price": short_tp_price,
            "main_label": main_label,
            "sub_label": sub_label,
        }
    ))


@app.get("/api/live-klines")
async def api_live_klines(
    symbol: str = Query(..., description="Bybit Symbol, z.B. FILUSDT"),
    interval: str = Query("5", description="Bybit Kline-Interval, z.B. 1,3,5,15"),
    limit: int = Query(200, ge=1, le=1000),
):
    """
    Liefert Kline-Daten für ein Symbol als JSON, damit das Frontend keinen
    direkten Request an Bybit (CORS / Browser) machen muss.
    """
    sym = (symbol or "").strip().upper()
    base_url = "https://api.bybit.com"

    async def _fetch(category: str) -> dict:
        url = f"{base_url}/v5/market/kline"
        params = {
            "category": category,
            "symbol": sym,
            "interval": interval,
            "limit": str(limit),
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()

    try:
        data = await _fetch("linear")
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            # Fallback auf spot, falls Symbol kein Linear-Future ist
            logger.info(f"[LIVE-KLINES] Linear-Klines leer/Fehler für {sym}, versuche spot. Ret={data.get('retCode')}")
            data = await _fetch("spot")

        if data.get("retCode") != 0:
            return JSONResponse(
                {"success": False, "error": data.get("retMsg", "Bybit-Fehler"), "retCode": data.get("retCode")},
                status_code=502,
            )

        raw_list = (data.get("result") or {}).get("list") or []
        candles = []
        for row in raw_list:
            try:
                ts_ms = int(row[0])
                candles.append(
                    {
                        "time": ts_ms // 1000,
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                    }
                )
            except Exception:
                continue

        candles.sort(key=lambda c: c["time"])
        return {"success": True, "symbol": sym, "interval": interval, "candles": candles}
    except Exception as e:
        logger.warning(f"[LIVE-KLINES] Fehler beim Laden der Klines für {sym}: {e}", exc_info=True)
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500,
        )


@app.get("/api/live-positions")
async def api_live_positions(
    symbol: str = Query(..., description="Bybit Symbol, z.B. FILUSDT"),
    account: str = Query("main", description="main|sub|Long_bot_N|Short_bot_N"),
):
    """
    Liefert die aktuellen Entry-Preise (Long/Short) und den aktuellen Mark-Preis
    für ein Symbol. account wählbar: main, sub oder ein Long/Short-Bot.
    """
    sym = (symbol or "").strip().upper()
    acc, _ = _normalize_live_charts_account(account)
    try:
        live = _get_live_positions_snapshot(acc, sym, profile=None) or {}

        def _safe_float(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        long_entry = _safe_float((live.get("long") or {}).get("entry_price"))
        short_entry = _safe_float((live.get("short") or {}).get("entry_price"))

        # Short-TP aus Bot-State (order_config.current_short_tp_price)
        short_tp_price = None
        if acc in ("sub", "Short_bot_1", "Short_bot_2"):
            try:
                short_state = load_bot_state(sym, bot_type="short")
                order_cfg = short_state.get("order_config") or {}
                short_tp_price = _safe_float(order_cfg.get("current_short_tp_price"))
            except Exception:
                short_tp_price = None

        current_price = _safe_float(live.get("current_price"))

        return {
            "success": True,
            "symbol": sym,
            "account": acc,
            "long_entry_price": long_entry,
            "short_entry_price": short_entry,
            "short_tp_price": short_tp_price,
            "current_price": current_price,
        }
    except Exception as e:
        logger.warning(f"[LIVE-POSITIONS] Fehler beim Laden der Live-Positionen für {sym}: {e}", exc_info=True)
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500,
        )


@app.get("/api/live-positions-grid")
async def api_live_positions_grid(
    user: dict = Depends(require_auth),
    profile: Optional[str] = Query(None, description="Legacy: main|bot_1|bot_2"),
    account: Optional[str] = Query(None, description="main|sub|Long_bot_N|Short_bot_N für Einzelaccount"),
):
    """
    Liefert eine Kachel-Übersicht für Live-Charts.
    Bei account=Long_bot_1/Short_bot_1/…: nur dieser Account. Sonst bei profile=bot_1/bot_2: Main+Sub.
    """
    if not user:
        raise HTTPException(status_code=401, detail="Auth required")

    project_root = Path(__file__).resolve().parent.parent
    acc, _ = _normalize_live_charts_account(account)
    prof, bot_type, _ = _resolve_account_to_profile_and_type(acc)

    def _read_levels(sym: str, bt: str, prof_arg: str = "main") -> tuple[list[float], list[float]]:
        """Return (burn_levels_all_cycles, exit_levels)."""
        burn_levels: list[float] = []
        exit_levels: list[float] = []
        try:
            cfg_name = f"{bt}_config_{sym}.yaml"
            if prof_arg in ("bot_1", "bot_2"):
                cfg_path = project_root / "config" / prof_arg / cfg_name
                if not cfg_path.exists():
                    cfg_path = project_root / "config" / cfg_name
            else:
                cfg_path = project_root / "config" / cfg_name
            if not cfg_path.exists():
                return [], []
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}

            # burn_levels: cycle1 + next_cycles
            raw_levels = cfg.get("burn_levels") or []
            for x in raw_levels if isinstance(raw_levels, list) else []:
                if isinstance(x, (int, float)) and float(x) > 0:
                    burn_levels.append(float(x))
                elif isinstance(x, str) and x.strip():
                    try:
                        burn_levels.append(float(x.strip().replace(",", ".")))
                    except Exception:
                        pass
            for cycle in (cfg.get("next_cycles") or []):
                if not isinstance(cycle, dict):
                    continue
                raw = cycle.get("burn_levels") or []
                for x in raw if isinstance(raw, list) else []:
                    if isinstance(x, (int, float)) and float(x) > 0:
                        burn_levels.append(float(x))
                    elif isinstance(x, str) and x.strip():
                        try:
                            burn_levels.append(float(x.strip().replace(",", ".")))
                        except Exception:
                            pass
            seen: set[float] = set()
            burn_levels = [p for p in sorted(burn_levels) if p not in seen and not seen.add(p)]

            # exit_levels (TP Levels)
            raw_exit = cfg.get("exit_levels") or []
            for x in raw_exit if isinstance(raw_exit, list) else []:
                if isinstance(x, (int, float)):
                    v = float(x)
                elif isinstance(x, str) and x.strip():
                    try:
                        v = float(x.strip().replace(",", "."))
                    except Exception:
                        continue
                else:
                    continue
                if v > 0:
                    exit_levels.append(v)

            # Backward compat: fixed_price ohne exit_levels → fixed_price als TP1
            if not exit_levels:
                key = "long_tp_fixed_price" if bt == "long" else "short_tp_fixed_price"
                fp = cfg.get(key)
                try:
                    fpv = float(fp) if fp is not None and str(fp).strip() else None
                except Exception:
                    fpv = None
                if fpv and fpv > 0:
                    exit_levels = [fpv]
        except Exception:
            return [], []
        return burn_levels, exit_levels

    try:
        if single_account_mode:
            acc_map = _get_live_positions_all_snapshot(acc, profile=None) or {}
            tiles = []
            for sym, live in sorted(acc_map.items(), key=lambda kv: kv[0]):
                bl, el = _read_levels(sym, bt=bot_type, prof_arg=prof)
                tiles.append({
                    "symbol": sym,
                    "account": acc,
                    "current_price": live.get("current_price"),
                    "long": live.get("long"),
                    "short": live.get("short"),
                    "burn_levels": bl,
                    "exit_levels": el,
                })
            return {"success": True, "ts": time.time(), "main": tiles, "sub": [], "single_account": True}
    except Exception as e:
        logger.warning(f"[LIVE-GRID] Fehler beim Laden der Grid-Daten: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def _stop_bot_via_master_api(
    client: httpx.AsyncClient,
    symbol: str,
    bot_type: str
) -> None:
    """Stoppe ein laufendes Bot-Subsystem über die Master Bot API."""
    request_id = str(uuid.uuid4())
    logger.info(f"🛑 Stoppe bestehenden {bot_type}-Bot für {symbol} (Request-ID: {request_id})")
    response = await client.post(
        f"{MASTER_BOT_API_URL}/master/bots/stop",
        json={"symbol": symbol, "bot_type": bot_type},
        headers={
            "X-Request-ID": request_id,
            "X-Internal-Token": MASTER_BOT_API_TOKEN,
            "Content-Type": "application/json"
        }
    )

    if response.status_code != 200:
        try:
            error_payload = response.json()
            error_message = error_payload.get("message", response.text)
        except ValueError:
            error_message = response.text
        logger.error(
            f"❌ {bot_type.capitalize()}-Bot Stop fehlgeschlagen "
            f"(HTTP {response.status_code}): {error_message}"
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=f"{bot_type.capitalize()}-Bot konnte nicht gestoppt werden: {error_message}"
        )

    response_payload = response.json()
    if not response_payload.get("success"):
        error_message = response_payload.get("message", "Unbekannter Fehler")
        logger.error(f"❌ {bot_type.capitalize()}-Bot Stop fehlgeschlagen: {error_message}")
        raise HTTPException(
            status_code=500,
            detail=f"{bot_type.capitalize()}-Bot konnte nicht gestoppt werden: {error_message}"
        )

    logger.info(f"✅ {bot_type.capitalize()}-Bot erfolgreich gestoppt für {symbol}")


async def _stop_existing_bots_before_open(
    symbol: str,
    client: httpx.AsyncClient
) -> List[str]:
    """Stoppe alle verbleibenden Bots für das Symbol (Long + Short) vor einem neuen Open."""
    stopped = []
    for bot_type in ("long", "short"):
        if is_bot_running(symbol, bot_type=bot_type):
            await _stop_bot_via_master_api(client, symbol, bot_type)
            stopped.append(bot_type)
    return stopped


@app.post("/api/hedge/open-positions")
async def api_open_hedged_positions(
    request: Request,
    user: dict = Depends(require_auth),
    data: dict = Body(...)
):
    """Open Long+Short positions simultaneously"""
    logger.info("=" * 80)
    logger.info("🔵🔴 BUTTON GEDRÜCKT: Positionen öffnen")
    logger.info("=" * 80)
    logger.info(f"📥 Empfangene Daten: {data}")
    
    symbol = data.get("symbol", "").upper().strip()
    long_notional = data.get("long_notional")
    short_notional = data.get("short_notional")
    
    logger.info(f"📊 Extrahierte Werte:")
    logger.info(f"   - Symbol: {symbol}")
    logger.info(f"   - Long Notional: {long_notional}")
    logger.info(f"   - Short Notional: {short_notional}")
    
    if not symbol:
        logger.error("❌ Validierung fehlgeschlagen: Symbol fehlt")
        raise HTTPException(status_code=400, detail="Symbol ist erforderlich")
    
    # Wenn Size-Felder leer: abgerundete Wallet * 20% verwenden
    need_long_from_wallet = long_notional is None or str(long_notional).strip() == "" or float(long_notional or 0) <= 0
    need_short_from_wallet = short_notional is None or str(short_notional).strip() == "" or float(short_notional or 0) <= 0

    if need_long_from_wallet or need_short_from_wallet:
        main_api_key, main_secret_key = _get_account_keys("main")
        sub_api_key, sub_secret_key = _get_account_keys("sub")
        if not all([main_api_key, main_secret_key, sub_api_key, sub_secret_key]):
            raise HTTPException(status_code=400, detail="API-Keys fehlen für Wallet-Abruf")
        main_om = BybitOrderManager(main_api_key, main_secret_key)
        sub_om = BybitOrderManager(sub_api_key, sub_secret_key)
        try:
            main_bal, sub_bal = await asyncio.gather(
                asyncio.to_thread(main_om.get_account_margin_balance),
                asyncio.to_thread(sub_om.get_account_margin_balance)
            )
            if need_long_from_wallet and main_bal and main_bal > 0:
                long_notional = max(5.0, min(int(main_bal * 0.20), 50000))
                logger.info(f"📊 Long-Size aus Main-Wallet*20%: {long_notional:.0f} USDT (Balance: {main_bal:.2f})")
            elif need_long_from_wallet:
                raise HTTPException(status_code=400, detail="Long Notional erforderlich (Wallet-Balance konnte nicht ermittelt werden)")
            if need_short_from_wallet and sub_bal and sub_bal > 0:
                short_notional = max(5.0, min(int(sub_bal * 0.20), 50000))
                logger.info(f"📊 Short-Size aus Sub-Wallet*20%: {short_notional:.0f} USDT (Balance: {sub_bal:.2f})")
            elif need_short_from_wallet:
                raise HTTPException(status_code=400, detail="Short Notional erforderlich (Wallet-Balance konnte nicht ermittelt werden)")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Wallet-Fallback fehlgeschlagen: {e}", exc_info=True)
            raise HTTPException(status_code=400, detail="Wallet-Abruf fehlgeschlagen – bitte Size manuell eingeben")

    try:
        long_notional = float(long_notional)
        short_notional = float(short_notional)
        logger.info(f"✅ Werte: Long={long_notional}$, Short={short_notional}$")
    except ValueError as e:
        logger.error(f"❌ Fehler beim Konvertieren der Werte: {e}")
        raise HTTPException(status_code=400, detail="Ungültige Werte")

    # Start-Size NICHT hier in Config schreiben: Dies ist ein einmaliges Öffnen mit dieser Size.
    # initial_short_usdt/initial_long_usdt sollen nur bei explizitem "Default-Start-Size" geändert
    # werden, damit Auto-Restart nach Short-TP wieder mit der gewünschten Start-Size (z. B. 400 USDT)
    # öffnet und nicht mit der letzten Ad-hoc-Size (z. B. 38 USDT).

    request_id = str(uuid.uuid4())
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:  # 5 Minuten Timeout für VPN/Bybit API
            try:
                stopped_bots = await _stop_existing_bots_before_open(symbol, client)
                if stopped_bots:
                    logger.info(
                        f"🛑 Vorhandene Bot(s) für {symbol} gestoppt vor neuer Position: {', '.join(stopped_bots)}"
                    )

                with _START_GATE_LOCK:
                    if symbol in _START_GATE:
                        gate_entry = _START_GATE[symbol]
                        long_running = is_bot_running(symbol, bot_type="long")
                        short_running = is_bot_running(symbol, bot_type="short")
                        if not (long_running or short_running):
                            logger.info(f"[START-GATE] {symbol} previously started but no bot running anymore → cleanup entry")
                            del _START_GATE[symbol]
                        else:
                            logger.info(
                                f"[START-GATE] {symbol} already started (request_id={gate_entry['request_id']}) – skip bot start"
                            )
                            return gate_entry["response"]
                    logger.info(f"[START-GATE] Start issued for {symbol} (request_id={request_id})")

                logger.info("=" * 80)
                logger.info("🚀 Öffne gehedgte Positionen (via Master Bot API)")
                logger.info(f"   Symbol: {symbol}")
                logger.info(f"   Long: {long_notional}$")
                logger.info(f"   Short: {short_notional}$")
                logger.info("=" * 80)

                # ✅ WICHTIG: Lösche JSON-Dateien vor dem Öffnen neuer Positionen
                # Damit werden beim nächsten Start wieder config.yaml-Werte verwendet
                logger.info("🗑️ Lösche Order-Parameter-JSON-Dateien (neue Positionen → verwende config.yaml)...")
                delete_order_params(symbol, 'long')
                delete_order_params(symbol, 'short')
                logger.info("✅ Order-Parameter-JSON-Dateien gelöscht - Bots werden config.yaml verwenden")

                # Generiere Request-ID für Idempotenz
                request_id = str(uuid.uuid4())
                logger.info(f"📋 Request-ID: {request_id}")

                # Rufe Master Bot API auf
                logger.info(f"🌐 Rufe Master Bot API auf: {MASTER_BOT_API_URL}/master/open-hedge")

                response = await client.post(
                    f"{MASTER_BOT_API_URL}/master/open-hedge",
                    json={
                        "symbol": symbol,
                        "long_notional": long_notional,
                        "short_notional": short_notional
                    },
                    headers={
                        "X-Request-ID": request_id,
                        "X-Internal-Token": MASTER_BOT_API_TOKEN,
                        "Content-Type": "application/json"
                    }
                )

                logger.info(f"📥 API Response Status: {response.status_code}")

                # Prüfe HTTP-Status
                if response.status_code == 200:
                    api_response = response.json()
                    logger.info(f"📤 API Response: {api_response}")

                    # Prüfe ob API-Response erfolgreich war
                    if api_response.get("success"):
                        # API-Response hat bereits das richtige Format
                        data = api_response.get("data", {})
                        payload = {
                            "success": True,
                            "message": api_response.get("message", f"Positionen erfolgreich geöffnet für {symbol}"),
                            "data": {
                                "symbol": symbol,
                                "long_notional": long_notional,
                                "short_notional": short_notional,
                                "price": data.get('price'),
                                "long_size": data.get('long_size'),
                                "short_size": data.get('short_size'),
                                "long_order": data.get('long_order'),
                                "short_order": data.get('short_order')
                            }
                        }
                        with _START_GATE_LOCK:
                            _START_GATE[symbol] = {
                                "request_id": request_id,
                                "start_timestamp": time.time(),
                                "response": payload
                            }
                        return payload
                    else:
                        # API-Response hat Fehler
                        error_code = api_response.get("error_code", "UNKNOWN")
                        error_message = api_response.get("message", "Unknown error")
                        logger.error(f"❌ Master Bot API Error ({error_code}): {error_message}")

                        # Konvertiere API-Error-Codes zu HTTP-Status-Codes
                        if error_code == "LOCKED":
                            raise HTTPException(status_code=409, detail=error_message)
                        elif error_code == "INVALID_TOKEN":
                            raise HTTPException(status_code=401, detail=error_message)
                        elif error_code == "INVALID_STATE":
                            raise HTTPException(status_code=400, detail=error_message)
                        else:
                            raise HTTPException(status_code=500, detail=error_message)
                else:
                    # HTTP-Status != 200
                    try:
                        error_response = response.json()
                        error_message = error_response.get("message", f"HTTP {response.status_code}")
                        logger.error(f"❌ HTTP Error {response.status_code}: {error_message}")
                        raise HTTPException(status_code=response.status_code, detail=error_message)
                    except:
                        logger.error(f"❌ HTTP Error {response.status_code}: {response.text}")
                        raise HTTPException(status_code=response.status_code, detail=f"Master Bot API error: {response.text}")

            except httpx.HTTPError as e:
                logger.error(f"❌ HTTP-Error beim Aufruf der Master Bot API: {e}", exc_info=True)
                raise HTTPException(status_code=503, detail=f"Master Bot API nicht erreichbar: {str(e)}")
            except httpx.TimeoutException:
                logger.error(f"❌ Timeout beim Aufruf der Master Bot API")
                raise HTTPException(status_code=504, detail="Master Bot API Timeout")

    except HTTPException:
        # Re-raise HTTPExceptions (sind bereits korrekt formatiert)
        raise
    except Exception as e:
        logger.error(f"❌ Unerwarteter Fehler beim Öffnen der Positionen: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Fehler: {str(e)}")


@app.post("/api/hedge/positions-cache-clear")
async def api_hedge_positions_cache_clear(
    user: dict = Depends(require_auth),
    symbol: Optional[str] = Query(None),
    profile: Optional[str] = Query(None),
):
    """Leert den Positions-Cache (WebSocket/Throttling), damit der nächste Abruf frische Bybit-Daten liefert. Hilft bei Phantom-Positionen."""
    cleared = 0
    if symbol:
        sym = symbol.strip().upper()
        prof = (profile or "").strip()
        prof = prof if prof in ("bot_1", "bot_2") else None
        for k in list(LIVE_POSITION_CACHE.keys()):
            if sym not in k:
                continue
            if prof:
                if k.startswith(f"{prof}:") or prof in k:
                    LIVE_POSITION_CACHE.pop(k, None)
                    cleared += 1
            else:
                if not any(k.startswith(p + ":") for p in ("bot_1", "bot_2")):
                    LIVE_POSITION_CACHE.pop(k, None)
                    cleared += 1
    else:
        cleared = len(LIVE_POSITION_CACHE)
        LIVE_POSITION_CACHE.clear()
    return {"success": True, "cleared": cleared}


@app.get("/api/hedge/positions/{symbol}")
async def api_get_hedge_positions(
    symbol: str,
    user: dict = Depends(require_auth),
    profile: Optional[str] = Query(None, description="main|bot_1|bot_2 für Account-Auswahl"),
):
    """Get Long and Short position info for a symbol"""
    request_start_time = time.time()
    # Log IMMEDIATELY at the start - before any other code
    logger.info(f"[API] ========== GET /api/hedge/positions/{symbol} - Request gestartet ==========")
    logger.info(f"[API] User: {user.get('username', 'unknown') if user else 'None'}")
    logger.info(f"[API] Symbol: {symbol}")
    logger.debug(f"[API] GET /api/hedge/positions/{symbol} - Request gestartet (DEBUG)")
    import asyncio
    try:
        logger.info(f"[API] Schritt 0.1: Lade Account-Keys (profile={profile})...")
        if profile and profile in ("main", "bot_1", "bot_2"):
            main_api_key, main_secret_key, sub_api_key, sub_secret_key = _get_account_keys_by_profile(profile)
        else:
            main_api_key, main_secret_key = _get_account_keys("main")
            sub_api_key, sub_secret_key = _get_account_keys("sub")
        
        if not all([main_api_key, main_secret_key, sub_api_key, sub_secret_key]):
            return {"success": False, "error": "API-Keys fehlen"}
        
        # Create Order Managers
        logger.info(f"[API] Schritt 0.4: Erstelle Order Managers...")
        main_order_manager = BybitOrderManager(main_api_key, main_secret_key)
        sub_order_manager = BybitOrderManager(sub_api_key, sub_secret_key)
        logger.info(f"[API] Schritt 0.5: Order Managers erstellt (Main + Sub mit je eigenem API-Key)")
        
        # OPTIMIERUNG: Nutze fetch_positions() - gibt ALLE Daten in einem Call zurück!
        # Statt 9 separate Calls (get_current_price, get_long_position, get_long_pnl, etc.)
        # machen wir nur 2 Calls: fetch_positions für Main + Sub Account
        # Das reduziert die Zeit von 2-3 Minuten auf ~30-40 Sekunden!
        try:
            logger.info(f"[API] OPTIMIERT: Hole alle Positions-Daten in 2 parallelen Calls für {symbol}...")
            fetch_start = time.time()
            
            # Parallele Calls für Main (Long) und Sub (Short) Account
            async def fetch_main_positions():
                """Holt alle Long-Position-Daten vom Main Account"""
                try:
                    # WICHTIG: Verwende fetch_positions_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                    positions = await asyncio.wait_for(
                        asyncio.to_thread(main_order_manager.fetch_positions_direct, symbol, 5),
                        timeout=10.0
                    )
                    return positions
                except Exception as e:
                    logger.error(f"[API] Fehler beim Abrufen der Main-Positionen: {e}", exc_info=True)
                    return []
            
            async def fetch_sub_positions():
                """Holt alle Short-Position-Daten vom Sub Account"""
                try:
                    # WICHTIG: Verwende fetch_positions_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                    positions = await asyncio.wait_for(
                        asyncio.to_thread(sub_order_manager.fetch_positions_direct, symbol, 5),
                        timeout=10.0
                    )
                    return positions
                except Exception as e:
                    logger.error(f"[API] Fehler beim Abrufen der Sub-Positionen: {e}", exc_info=True)
                    return []
            
            # Führe beide Calls parallel aus
            main_positions, sub_positions = await asyncio.gather(
                fetch_main_positions(),
                fetch_sub_positions()
            )
            
            fetch_duration = time.time() - fetch_start
            logger.info(f"[API] ✅ Beide Positions-Calls abgeschlossen (Dauer: {fetch_duration:.2f}s) | main={len(main_positions)} sub={len(sub_positions)}")

            # Zusätzliche Debug-Infos: kleine Stichprobe der Roh-Positionen loggen,
            # um besser zu sehen, was Bybit tatsächlich zurückliefert (insb. für bot_1/bot_2).
            def _debug_sample(label: str, positions_list):
                try:
                    sample = []
                    for p in (positions_list or [])[:3]:
                        info = p.get("info") if isinstance(p.get("info"), dict) else p
                        sample.append({
                            "symbol": info.get("symbol"),
                            "side": info.get("side"),
                            "size": info.get("size"),
                            "positionIdx": info.get("positionIdx"),
                        })
                    logger.info(f"[API] DEBUG positions({label}) sample: {sample}")
                except Exception:
                    logger.debug(f"[API] DEBUG positions({label}) sample logging failed", exc_info=True)

            _debug_sample("main", main_positions)
            _debug_sample("sub", sub_positions)
            
            # _parse_positions wie in letzten funktionierenden Commits (ed657cd, a7f6119, 77ac4d7)
            def _parse_positions(positions):
                parsed = {
                    "long": None,
                    "short": None,
                    "current_price": None
                }
                for pos in positions:
                    info = pos.get("info", {})
                    side = info.get("side", "")
                    size = float(info.get("size", 0) or 0)
                    pos_idx = int(info.get("positionIdx", 0))
                    if size <= 0:
                        continue

                    entry_price = float(info.get("avgPrice") or info.get("entryPrice") or 0)
                    unrealised_pnl = float(info.get("unrealisedPnl", 0) or 0)
                    mark_price = float(info.get("markPrice", 0) or 0)
                    realised_raw = (
                        info.get("curRealisedPnl")
                        or info.get("cumRealisedPnl")
                        or info.get("realisedPnl")
                        or info.get("closedPnl")
                        or 0
                    )
                    try:
                        realised_pnl = float(realised_raw)
                    except (ValueError, TypeError):
                        realised_pnl = 0.0

                    if not parsed["current_price"] and mark_price > 0:
                        parsed["current_price"] = mark_price

                    tp_price = None
                    sl_price = None
                    tp_price_str = info.get("takeProfit", None)
                    sl_price_str = info.get("stopLoss", None)
                    if tp_price_str:
                        try:
                            tp_price = float(tp_price_str)
                        except (ValueError, TypeError):
                            tp_price = None
                    if sl_price_str:
                        try:
                            sl_price = float(sl_price_str)
                        except (ValueError, TypeError):
                            sl_price = None

                    position_value_raw = info.get("positionValue")
                    try:
                        position_value = float(position_value_raw) if position_value_raw is not None and position_value_raw != "" else None
                    except (ValueError, TypeError):
                        position_value = None
                    if entry_price <= 0 and position_value and size > 0:
                        entry_price = position_value / size

                    if side == "Buy" and (pos_idx == 1 or pos_idx == 0) and parsed["long"] is None:
                        parsed["long"] = {
                            "size": size,
                            "entry_price": entry_price,
                            "unrealised_pnl": unrealised_pnl,
                            "realised_pnl": realised_pnl,
                            "mark_price": mark_price,
                            "position_value": position_value,
                            "tp_price": tp_price,
                            "sl_price": sl_price,
                            "pos_idx": pos_idx
                        }
                    elif side == "Sell" and (pos_idx == 2 or pos_idx == 0) and parsed["short"] is None:
                        parsed["short"] = {
                            "size": size,
                            "entry_price": entry_price,
                            "unrealised_pnl": unrealised_pnl,
                            "realised_pnl": realised_pnl,
                            "mark_price": mark_price,
                            "position_value": position_value,
                            "tp_price": tp_price,
                            "sl_price": sl_price,
                            "pos_idx": pos_idx
                        }
                return parsed

            main_parsed = _parse_positions(main_positions)
            sub_parsed = _parse_positions(sub_positions)

            current_price = main_parsed["current_price"] or sub_parsed["current_price"]

            # Debug: Parsed-Ergebnisse (Main/Sub Long/Short)
            _ml, _ms = main_parsed.get("long"), main_parsed.get("short")
            _sl, _ss = sub_parsed.get("long"), sub_parsed.get("short")
            logger.info(
                "[API] DEBUG parsed profile=%s: main_long=%s main_short=%s | sub_long=%s sub_short=%s",
                profile,
                _ml.get("size") if _ml else None,
                _ms.get("size") if _ms else None,
                _sl.get("size") if _sl else None,
                _ss.get("size") if _ss else None,
            )

            # Main Account: Long aus Main, Short aus Sub (Fallback: Short aus Main wenn Sub leer)
            long_source = main_parsed["long"]
            short_source = sub_parsed["short"]
            if not short_source and main_parsed.get("short"):
                short_source = main_parsed["short"]
                logger.info("[API] DEBUG short von main uebernommen (sub leer): size=%s", short_source.get("size"))
            long_account = "main" if long_source else None
            short_account = "sub" if short_source else None

            long_size = long_source["size"] if long_source else None
            long_entry_price = long_source["entry_price"] if long_source else None
            long_tp_price = long_source["tp_price"] if long_source else None
            long_sl_price = long_source["sl_price"] if long_source else None
            long_pos_idx = long_source["pos_idx"] if long_source else None
            long_pnl_data = {
                "unrealised_pnl": long_source["unrealised_pnl"],
                "realised_pnl": long_source["realised_pnl"],
                "mark_price": long_source["mark_price"],
                "size": long_source["size"],
                "entry_price": long_source["entry_price"],
                "position_value": long_source.get("position_value")
            } if long_source else None

            short_size = short_source["size"] if short_source else None
            short_entry_price = short_source["entry_price"] if short_source else None
            short_tp_price = short_source["tp_price"] if short_source else None
            short_sl_price = short_source["sl_price"] if short_source else None
            short_pos_idx = short_source["pos_idx"] if short_source else None
            short_pnl_data = {
                "unrealised_pnl": short_source["unrealised_pnl"],
                "realised_pnl": short_source["realised_pnl"],
                "mark_price": short_source["mark_price"],
                "size": short_source["size"],
                "entry_price": short_source["entry_price"],
                "position_value": short_source.get("position_value")
            } if short_source else None

            if long_source:
                logger.info(f"[API] Main Long: size={long_size}, entry={long_entry_price}, pnl={long_source['unrealised_pnl']}")
            if short_source:
                logger.info(f"[API] Main Short: size={short_size}, entry={short_entry_price}, pnl={short_source['unrealised_pnl']}")
            
            # TP/SL aus Open Orders nur für Main-Positionen (Main-Account), nicht von Sub übernehmen
            tp_sl_fetch_tasks = []
            if long_source and (not long_tp_price or not long_sl_price):
                logger.info(f"[API] TP/SL fehlen (Main Long), hole aus Open Orders...")
                long_position_idx = long_pos_idx if long_pos_idx is not None else 1
                tp_sl_fetch_tasks.append(("long", asyncio.wait_for(
                    asyncio.to_thread(main_order_manager.get_tp_sl_orders, symbol, position_idx=long_position_idx),
                    timeout=20.0
                )))
            if short_source and (not short_tp_price or not short_sl_price):
                logger.info(f"[API] TP/SL fehlen (Main Short), hole aus Open Orders...")
                short_position_idx = short_pos_idx if short_pos_idx is not None else 2
                tp_sl_fetch_tasks.append(("short", asyncio.wait_for(
                    asyncio.to_thread(main_order_manager.get_tp_sl_orders, symbol, position_idx=short_position_idx),
                    timeout=20.0
                )))
            
            # Führe alle TP/SL-Fetches parallel aus (falls nötig)
            if tp_sl_fetch_tasks:
                try:
                    results = await asyncio.gather(*[task[1] for task in tp_sl_fetch_tasks], return_exceptions=True)
                    for i, (task_type, _) in enumerate(tp_sl_fetch_tasks):
                        if isinstance(results[i], Exception):
                            logger.warning(f"[API] Fehler beim Abrufen der {task_type} TP/SL Orders: {results[i]}")
                            continue
                        
                        tp_sl_data = results[i]
                        if tp_sl_data:
                            if task_type == "long":
                                if not long_tp_price and tp_sl_data.get('tp_prices'):
                                    long_tp_price = tp_sl_data['tp_prices'][0] if tp_sl_data['tp_prices'] else None
                                    logger.info(f"[API] Long-TP aus Open Orders: {long_tp_price}")
                                if not long_sl_price and tp_sl_data.get('sl_prices'):
                                    long_sl_price = tp_sl_data['sl_prices'][0] if tp_sl_data['sl_prices'] else None
                                    logger.info(f"[API] Long-SL aus Open Orders: {long_sl_price}")
                            elif task_type == "short":
                                if not short_tp_price and tp_sl_data.get('tp_prices'):
                                    short_tp_price = tp_sl_data['tp_prices'][0] if tp_sl_data['tp_prices'] else None
                                    logger.info(f"[API] Short-TP aus Open Orders: {short_tp_price}")
                                if not short_sl_price and tp_sl_data.get('sl_prices'):
                                    short_sl_price = tp_sl_data['sl_prices'][0] if tp_sl_data['sl_prices'] else None
                                    logger.info(f"[API] Short-SL aus Open Orders: {short_sl_price}")
                except Exception as e:
                    logger.warning(f"[API] Fehler beim parallelen Abrufen der TP/SL Orders: {e}")
            
            # Fallback: Wenn current_price noch nicht gesetzt, hole es separat
            if not current_price:
                logger.warning(f"[API] markPrice nicht verfügbar, hole current_price separat...")
                try:
                    current_price = await asyncio.wait_for(
                        asyncio.to_thread(main_order_manager.get_current_price, symbol),
                        timeout=30.0
                    )
                except:
                    current_price = 0.0
            
            # Erstelle TP/SL Orders-Daten im erwarteten Format
            long_tp_sl_orders = {
                'tp_count': 1 if long_tp_price else 0,
                'sl_count': 1 if long_sl_price else 0,
                'tp_prices': [long_tp_price] if long_tp_price else [],
                'sl_prices': [long_sl_price] if long_sl_price else []
            }
            
            short_tp_sl_orders = {
                'tp_count': 1 if short_tp_price else 0,
                'sl_count': 1 if short_sl_price else 0,
                'tp_prices': [short_tp_price] if short_tp_price else [],
                'sl_prices': [short_sl_price] if short_sl_price else []
            }
            
            logger.info(f"[API] ✅ Alle Positions-Daten geparst: current_price={current_price}, long_size={long_size}, short_size={short_size}")
        except asyncio.TimeoutError:
            request_duration = time.time() - request_start_time
            logger.warning(f"[API] Timeout beim Abrufen der Positionen für {symbol} nach {request_duration:.2f}s - gebe leere Daten zurück")
            # Gebe leere Daten zurück statt Fehler, damit Dashboard weiterläuft
            return {
                "success": True,
                "symbol": symbol,
                "current_price": 0.0,
                "long": {"exists": False},
                "short": {"exists": False},
                "total_pnl": 0.0
            }
        except requests.exceptions.RequestException as e:
            request_duration = time.time() - request_start_time
            logger.warning(f"[API] Request-Fehler beim Abrufen der Positionen für {symbol} nach {request_duration:.2f}s: {e}", exc_info=True)
            # Gebe leere Daten zurück statt Fehler
            return {
                "success": True,
                "symbol": symbol,
                "current_price": 0.0,
                "long": {"exists": False},
                "short": {"exists": False},
                "total_pnl": 0.0
            }
        
        # Burn-Status immer aus Bot-State laden (für einheitliche Anzeige Main/Sub, auch ohne Position)
        long_bot_state = load_bot_state(symbol, bot_type="long")
        short_bot_state = load_bot_state(symbol, bot_type="short")
        long_burn_count = long_bot_state.get("burn_count", 0)
        long_burns_before_rebuy = long_bot_state.get("burns_before_rebuy", 4)
        short_burn_count = short_bot_state.get("burn_count", 0)
        short_burns_before_rebuy = short_bot_state.get("burns_before_rebuy", 4)

        # Calculate values
        long_data = None
        if long_size and long_entry_price:
            long_notional = (long_pnl_data.get("position_value") if long_pnl_data else None) or (long_size * long_entry_price)
            long_current_notional = long_size * current_price
            long_unrealised_pnl = long_pnl_data.get('unrealised_pnl', 0) if long_pnl_data else (current_price - long_entry_price) * long_size
            long_realised_pnl = long_pnl_data.get('realised_pnl', 0) if long_pnl_data else 0
            long_pnl_percentage = (long_unrealised_pnl / long_notional * 100) if long_notional > 0 else 0
            
            # Calculate TP progress
            long_tp_progress = None
            long_tp_percentage = None
            long_remaining_to_tp = None
            if long_tp_price and long_entry_price:
                # TP percentage from entry
                long_tp_percentage = ((long_tp_price - long_entry_price) / long_entry_price) * 100
                # Current progress percentage
                long_current_progress = ((current_price - long_entry_price) / long_entry_price) * 100
                # Remaining to TP
                long_remaining_to_tp = long_tp_percentage - long_current_progress
            
            # long_burn_count / long_burns_before_rebuy bereits oben geladen (Zeile ~1442–1446)
            # Calculate price deviation from entry (for Long: positive when price is above entry)
            long_price_deviation = ((current_price - long_entry_price) / long_entry_price * 100) if long_entry_price > 0 else 0
            
            # Prepare TP/SL orders data
            long_tp_orders_data = None
            if long_tp_sl_orders:
                long_tp_orders_data = {
                    "tp_count": long_tp_sl_orders.get("tp_count", 0),
                    "sl_count": long_tp_sl_orders.get("sl_count", 0),
                    "tp_prices": [round(p, 6) for p in long_tp_sl_orders.get("tp_prices", [])],
                    "sl_prices": [round(p, 6) for p in long_tp_sl_orders.get("sl_prices", [])],
                    "tp_orders": [
                        {
                            "price": round(o["price"], 6),
                            "size": round(o["size"], 6),
                            "type": o["type"]
                        }
                        for o in long_tp_sl_orders.get("tp_orders", [])
                    ],
                    "sl_orders": [
                        {
                            "price": round(o["price"], 6),
                            "size": round(o["size"], 6),
                            "type": o["type"]
                        }
                        for o in long_tp_sl_orders.get("sl_orders", [])
                    ]
                }
            
            # TP − SL (Gegenseite): Netto-Gewinn wenn Long bei TP und Short bei SL schließt
            long_tp_minus_sl_usdt = None
            if long_tp_price and long_size and long_entry_price:
                tp_pnl_long = long_size * (long_tp_price - long_entry_price)
                if short_sl_price and short_size and short_entry_price:
                    sl_pnl_short = short_size * (short_entry_price - short_sl_price)  # negativ bei Verlust
                    long_tp_minus_sl_usdt = round(tp_pnl_long + sl_pnl_short, 2)
                else:
                    long_tp_minus_sl_usdt = round(tp_pnl_long, 2)

            logger.info(f"[API] Schritt 4.1: Erstelle long_data mit exists=True (size={long_size})")
            # Main Long = Long-Bot → Burn aus long_bot_state (z.B. 0/3)
            long_data = {
                "size_coins": round(long_size, 6),
                "size_usdt": round(long_notional, 6),
                "entry_price": round(long_entry_price, 6),
                "current_price": round(current_price, 6),
                "unrealised_pnl": round(long_unrealised_pnl, 6),
                "realised_pnl": round(long_realised_pnl, 6),
                "pnl_percentage": round(long_pnl_percentage, 2),
                "price_deviation": round(long_price_deviation, 2),
                "tp_minus_sl_usdt": long_tp_minus_sl_usdt,
                "tp_price": round(long_tp_price, 6) if long_tp_price else None,
                "tp_percentage": round(long_tp_percentage, 2) if long_tp_percentage else None,
                "remaining_to_tp": round(long_remaining_to_tp, 2) if long_remaining_to_tp is not None else None,
                "burn_count": long_burn_count,
                "burns_before_rebuy": long_burns_before_rebuy,
                "tp_sl_orders": long_tp_orders_data,
                "exists": True
            }
            logger.info(f"[API] Schritt 4.2: long_data erstellt: exists={long_data.get('exists')}")
        else:
            logger.info(f"[API] Schritt 4.3: Keine Long-Position (long_size={long_size}) - setze exists=False")
            # Burn trotzdem setzen, damit Main Long-Karte immer 0/3 zeigt (nicht Sub 1/2)
            long_data = {
                "exists": False,
                "burn_count": long_burn_count,
                "burns_before_rebuy": long_burns_before_rebuy,
                "tp_minus_sl_usdt": None
            }
        
        short_data = None
        if short_size and short_entry_price:
            short_notional = (short_pnl_data.get("position_value") if short_pnl_data else None) or (short_size * short_entry_price)
            short_current_notional = short_size * current_price
            short_unrealised_pnl = short_pnl_data.get('unrealised_pnl', 0) if short_pnl_data else (short_entry_price - current_price) * short_size
            short_realised_pnl = short_pnl_data.get('realised_pnl', 0) if short_pnl_data else 0
            short_pnl_percentage = (short_unrealised_pnl / short_notional * 100) if short_notional > 0 else 0
            
            # Calculate TP progress
            short_tp_progress = None
            short_tp_percentage = None
            short_remaining_to_tp = None
            if short_tp_price and short_entry_price:
                # TP percentage from entry (negative for short)
                short_tp_percentage = ((short_entry_price - short_tp_price) / short_entry_price) * 100
                # Current progress percentage
                short_current_progress = ((short_entry_price - current_price) / short_entry_price) * 100
                # Remaining to TP
                short_remaining_to_tp = short_tp_percentage - short_current_progress
            
            # Burn-Status bereits oben geladen (short_burn_count / short_burns_before_rebuy)

            # Calculate price deviation from entry (for Short: positive when price is below entry)
            short_price_deviation = ((short_entry_price - current_price) / short_entry_price * 100) if short_entry_price > 0 else 0
            
            # Prepare TP/SL orders data
            short_tp_orders_data = None
            if short_tp_sl_orders:
                short_tp_orders_data = {
                    "tp_count": short_tp_sl_orders.get("tp_count", 0),
                    "sl_count": short_tp_sl_orders.get("sl_count", 0),
                    "tp_prices": [round(p, 6) for p in short_tp_sl_orders.get("tp_prices", [])],
                    "sl_prices": [round(p, 6) for p in short_tp_sl_orders.get("sl_prices", [])],
                    "tp_orders": [
                        {
                            "price": round(o["price"], 6),
                            "size": round(o["size"], 6),
                            "type": o["type"]
                        }
                        for o in short_tp_sl_orders.get("tp_orders", [])
                    ],
                    "sl_orders": [
                        {
                            "price": round(o["price"], 6),
                            "size": round(o["size"], 6),
                            "type": o["type"]
                        }
                        for o in short_tp_sl_orders.get("sl_orders", [])
                    ]
                }
            
            # TP − SL (Gegenseite): Netto wenn Short bei TP und Long bei SL schließt
            short_tp_minus_sl_usdt = None
            if short_tp_price and short_size and short_entry_price:
                tp_pnl_short = short_size * (short_entry_price - short_tp_price)
                if long_sl_price and long_size and long_entry_price:
                    sl_pnl_long = long_size * (long_sl_price - long_entry_price)  # negativ bei Verlust
                    short_tp_minus_sl_usdt = round(tp_pnl_short + sl_pnl_long, 2)
                else:
                    short_tp_minus_sl_usdt = round(tp_pnl_short, 2)

            # Main: Beide Karten (Long + Short) denselben Burn wie Long-Bot (wie Sub beide Short-Bot-Burn)
            short_data = {
                "size_coins": round(short_size, 6),
                "size_usdt": round(short_notional, 6),
                "entry_price": round(short_entry_price, 6),
                "current_price": round(current_price, 6),
                "unrealised_pnl": round(short_unrealised_pnl, 6),
                "realised_pnl": round(short_realised_pnl, 6),
                "pnl_percentage": round(short_pnl_percentage, 2),
                "price_deviation": round(short_price_deviation, 2),
                "tp_minus_sl_usdt": short_tp_minus_sl_usdt,
                "tp_price": round(short_tp_price, 6) if short_tp_price else None,
                "tp_percentage": round(short_tp_percentage, 2) if short_tp_percentage else None,
                "remaining_to_tp": round(short_remaining_to_tp, 2) if short_remaining_to_tp is not None else None,
                "burn_count": long_burn_count,
                "burns_before_rebuy": long_burns_before_rebuy,
                "tp_sl_orders": short_tp_orders_data,
                "exists": True
            }
            logger.info(f"[API] Schritt 5.1: short_data erstellt: exists={short_data.get('exists')} (Main Burn einheitlich: {long_burn_count}/{long_burns_before_rebuy})")
        else:
            logger.info(f"[API] Schritt 5.2: Keine Short-Position (short_size={short_size}) - setze exists=False")
            short_data = {
                "exists": False,
                "burn_count": long_burn_count,
                "burns_before_rebuy": long_burns_before_rebuy,
                "tp_minus_sl_usdt": None
            }

        def _build_account_side_data(side_source, *, side: str, bot_type: str):
            if not side_source:
                return {"exists": False}
            size = side_source.get("size")
            entry_price = side_source.get("entry_price")
            position_value_raw = side_source.get("position_value")
            try:
                position_value = float(position_value_raw) if position_value_raw is not None and position_value_raw != "" else None
            except (TypeError, ValueError):
                position_value = None
            if (entry_price is None or entry_price <= 0) and size and position_value:
                entry_price = position_value / float(size)
            if size is None or entry_price is None:
                return {"exists": False}
            size = float(size)
            entry_price = float(entry_price)
            if size <= 0 or entry_price <= 0:
                return {"exists": False}

            notional = position_value or (size * entry_price)
            unrealised_pnl = side_source.get("unrealised_pnl")
            if unrealised_pnl is None:
                if side == "long":
                    unrealised_pnl = (current_price - entry_price) * size
                else:
                    unrealised_pnl = (entry_price - current_price) * size
            realised_pnl = side_source.get("realised_pnl") or 0.0
            pnl_percentage = (unrealised_pnl / notional * 100) if notional > 0 else 0.0

            tp_price = side_source.get("tp_price")
            sl_price = side_source.get("sl_price")
            tp_percentage = None
            remaining_to_tp = None
            if tp_price and entry_price:
                if side == "long":
                    tp_percentage = ((tp_price - entry_price) / entry_price) * 100
                    current_progress = ((current_price - entry_price) / entry_price) * 100
                else:
                    tp_percentage = ((entry_price - tp_price) / entry_price) * 100
                    current_progress = ((entry_price - current_price) / entry_price) * 100
                remaining_to_tp = tp_percentage - current_progress

            if side == "long":
                price_deviation = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0.0
            else:
                price_deviation = ((entry_price - current_price) / entry_price * 100) if entry_price > 0 else 0.0

            bot_state = load_bot_state(symbol, bot_type=bot_type)
            burn_count = bot_state.get("burn_count", 0)
            burns_before_rebuy = bot_state.get("burns_before_rebuy", 4)

            tp_sl_orders = {
                "tp_count": 1 if tp_price else 0,
                "sl_count": 1 if sl_price else 0,
                "tp_prices": [round(tp_price, 6)] if tp_price else [],
                "sl_prices": [round(sl_price, 6)] if sl_price else [],
                "tp_orders": [],
                "sl_orders": []
            }

            return {
                "size_coins": round(size, 6),
                "size_usdt": round(notional, 6),
                "entry_price": round(entry_price, 6),
                "current_price": round(current_price, 6),
                "unrealised_pnl": round(float(unrealised_pnl), 6),
                "realised_pnl": round(float(realised_pnl), 6),
                "pnl_percentage": round(float(pnl_percentage), 2),
                "price_deviation": round(float(price_deviation), 2),
                "tp_price": round(tp_price, 6) if tp_price else None,
                "tp_percentage": round(tp_percentage, 2) if tp_percentage is not None else None,
                "remaining_to_tp": round(remaining_to_tp, 2) if remaining_to_tp is not None else None,
                "burn_count": burn_count,
                "burns_before_rebuy": burns_before_rebuy,
                "tp_sl_orders": tp_sl_orders,
                "exists": True
            }

        sub_long_data = _build_account_side_data(sub_parsed.get("long"), side="long", bot_type="long")
        sub_short_data = _build_account_side_data(sub_parsed.get("short"), side="short", bot_type="short")
        # Sub Account: Burn ist global für den Short-Bot – beide Karten (Long + Short) zeigen denselben Wert
        if sub_long_data.get("exists"):
            sub_long_data["burn_count"] = short_burn_count
            sub_long_data["burns_before_rebuy"] = short_burns_before_rebuy
        if sub_short_data.get("exists"):
            sub_short_data["burn_count"] = short_burn_count
            sub_short_data["burns_before_rebuy"] = short_burns_before_rebuy

        # Sub: TP − SL (Gegenseite) für Long- und Short-Karte
        if sub_long_data.get("exists") and sub_short_data.get("exists"):
            tpo_long = sub_long_data.get("tp_sl_orders") or {}
            tpo_short = sub_short_data.get("tp_sl_orders") or {}
            long_tp = (tpo_long.get("tp_prices") or [None])[0] if tpo_long.get("tp_prices") else None
            short_sl = (tpo_short.get("sl_prices") or [None])[0] if tpo_short.get("sl_prices") else None
            if long_tp and short_sl:
                tp_pnl = sub_long_data["size_coins"] * (long_tp - sub_long_data["entry_price"])
                sl_pnl = sub_short_data["size_coins"] * (sub_short_data["entry_price"] - short_sl)
                sub_long_data["tp_minus_sl_usdt"] = round(tp_pnl + sl_pnl, 2)
            else:
                sub_long_data["tp_minus_sl_usdt"] = round(sub_long_data["size_coins"] * (long_tp - sub_long_data["entry_price"]), 2) if long_tp else None
            short_tp = (tpo_short.get("tp_prices") or [None])[0] if tpo_short.get("tp_prices") else None
            long_sl = (tpo_long.get("sl_prices") or [None])[0] if tpo_long.get("sl_prices") else None
            if short_tp and long_sl:
                tp_pnl_s = sub_short_data["size_coins"] * (sub_short_data["entry_price"] - short_tp)
                sl_pnl_l = sub_long_data["size_coins"] * (long_sl - sub_long_data["entry_price"])
                sub_short_data["tp_minus_sl_usdt"] = round(tp_pnl_s + sl_pnl_l, 2)
            else:
                sub_short_data["tp_minus_sl_usdt"] = round(sub_short_data["size_coins"] * (sub_short_data["entry_price"] - short_tp), 2) if short_tp else None
        else:
            if sub_long_data.get("exists"):
                tpo = sub_long_data.get("tp_sl_orders") or {}
                long_tp = (tpo.get("tp_prices") or [None])[0] if tpo.get("tp_prices") else None
                sub_long_data["tp_minus_sl_usdt"] = round(sub_long_data["size_coins"] * (long_tp - sub_long_data["entry_price"]), 2) if long_tp else None
            if sub_short_data.get("exists"):
                tpo = sub_short_data.get("tp_sl_orders") or {}
                short_tp = (tpo.get("tp_prices") or [None])[0] if tpo.get("tp_prices") else None
                sub_short_data["tp_minus_sl_usdt"] = round(sub_short_data["size_coins"] * (sub_short_data["entry_price"] - short_tp), 2) if short_tp else None

        # Calculate total unrealised PnL (wie bisher)
        total_pnl = 0
        if long_data.get("exists"):
            total_pnl += long_data.get("unrealised_pnl", 0)
        if short_data.get("exists"):
            total_pnl += short_data.get("unrealised_pnl", 0)
        if sub_long_data.get("exists"):
            total_pnl += sub_long_data.get("unrealised_pnl", 0)
        if sub_short_data.get("exists"):
            total_pnl += sub_short_data.get("unrealised_pnl", 0)

        # Aktueller Netto‑PnL pro Hedge (Realised + Unrealised)
        main_current_netto = 0.0
        if long_data.get("exists"):
            main_current_netto += long_data.get("unrealised_pnl", 0.0) + long_data.get("realised_pnl", 0.0)
        if short_data.get("exists"):
            main_current_netto += short_data.get("unrealised_pnl", 0.0) + short_data.get("realised_pnl", 0.0)
        main_current_netto = round(main_current_netto, 2)

        sub_current_netto = 0.0
        if sub_long_data.get("exists"):
            sub_current_netto += sub_long_data.get("unrealised_pnl", 0.0) + sub_long_data.get("realised_pnl", 0.0)
        if sub_short_data.get("exists"):
            sub_current_netto += sub_short_data.get("unrealised_pnl", 0.0) + sub_short_data.get("realised_pnl", 0.0)
        sub_current_netto = round(sub_current_netto, 2)

        # Diesen Netto‑Wert an beide Karten des jeweiligen Hedges anhängen
        if long_data.get("exists"):
            long_data["current_net_pnl_usdt"] = main_current_netto
        if short_data.get("exists"):
            short_data["current_net_pnl_usdt"] = main_current_netto
        if sub_long_data.get("exists"):
            sub_long_data["current_net_pnl_usdt"] = sub_current_netto
        if sub_short_data.get("exists"):
            sub_short_data["current_net_pnl_usdt"] = sub_current_netto
        
        request_duration = time.time() - request_start_time
        logger.info(f"[API] ========== GET /api/hedge/positions/{symbol} - Erfolgreich abgeschlossen (Dauer: {request_duration:.2f}s) ==========")
        logger.info("[API] Response profile=%s: long.exists=%s size=%s pnl=%s | short.exists=%s size=%s pnl=%s",
            profile, long_data.get("exists"), long_data.get("size_coins"), long_data.get("unrealised_pnl"),
            short_data.get("exists") if short_data else False, short_data.get("size_coins") if short_data else None,
            short_data.get("unrealised_pnl") if short_data else None)
        logger.info(f"[API] Response: success=True, symbol={symbol}, current_price={round(current_price, 6)}")
        logger.info(f"[API] Response long_data keys: {list(long_data.keys()) if long_data else 'None'}")
        logger.info(f"[API] Response short_data keys: {list(short_data.keys()) if short_data else 'None'}")
        logger.debug(f"[API] GET /api/hedge/positions/{symbol} - Erfolgreich abgeschlossen (Dauer: {request_duration:.2f}s, DEBUG)")
        # Main = Long-Bot: Beide Karten (Long + Short) denselben Burn wie Sub (beide gleicher Wert)
        main_burn_count = long_burn_count
        main_burns_before_rebuy = long_burns_before_rebuy
        main_long_burn = {"burn_count": main_burn_count, "burns_before_rebuy": main_burns_before_rebuy}
        main_short_burn = {"burn_count": main_burn_count, "burns_before_rebuy": main_burns_before_rebuy}
        response_data = {
            "success": True,
            "symbol": symbol,
            "current_price": round(current_price, 6),
            "long": long_data,
            "short": short_data,
            "main_long_burn": main_long_burn,
            "main_short_burn": main_short_burn,
            "sub": {
                "long": sub_long_data,
                "short": sub_short_data
            },
            "total_pnl": round(total_pnl, 2)
        }
        logger.info(f"[API] Response JSON (erste 500 Zeichen): {str(response_data)[:500]}")
        return response_data
        
    except Exception as e:
        request_duration = time.time() - request_start_time
        logger.error(f"[API] ========== FEHLER beim Abrufen der Positionen für {symbol} nach {request_duration:.2f}s ==========")
        logger.error(f"[API] Exception: {e}", exc_info=True)
        import traceback
        logger.error(f"[API] Traceback: {traceback.format_exc()}")
        return {"success": False, "error": str(e)}


@app.get("/api/hedge/positions-debug/{symbol}")
async def api_get_hedge_positions_debug(
    symbol: str,
    user: dict = Depends(require_auth),
    profile: Optional[str] = Query(None, description="main|bot_1|bot_2 – für profil-spezifische Positions-Debug-Infos"),
):
    """
    Debug: Ruft nur die Roh-Positionen von Bybit ab und gibt Anzahlen
    sowie Stichproben zurück (ohne sensible Daten).

    - Ohne profile: wie bisher Main+Sub (master/sub).
    - Mit profile=bot_1/bot_2: verwendet die in config.yaml hinterlegten Profil-Accounts
      (z. B. Long_bot_1/Short_bot_1), also exakt dieselben Keys wie die Bots.
    """
    try:
        if profile and profile in ("main", "bot_1", "bot_2"):
            long_key, long_sec, short_key, short_sec = _get_account_keys_by_profile(profile)
            if not all([long_key, long_sec, short_key, short_sec]):
                return {
                    "ok": False,
                    "error": f"API-Keys fehlen für profile={profile}",
                    "main_count": 0,
                    "sub_count": 0,
                    "profile": profile,
                }
            main_api_key, main_secret_key = long_key, long_sec
            sub_api_key, sub_secret_key = short_key, short_sec
        else:
            main_api_key, main_secret_key = _get_account_keys("main")
            sub_api_key, sub_secret_key = _get_account_keys("sub")
        if not all([main_api_key, main_secret_key, sub_api_key, sub_secret_key]):
            return {"ok": False, "error": "API-Keys fehlen", "main_count": 0, "sub_count": 0}

        main_om = BybitOrderManager(main_api_key, main_secret_key)
        sub_om = BybitOrderManager(sub_api_key, sub_secret_key)
        main_positions = await asyncio.to_thread(main_om.fetch_positions_direct, symbol, 5)
        sub_positions = await asyncio.to_thread(sub_om.fetch_positions_direct, symbol, 5)

        def sample(pos_list):
            out = []
            for p in (pos_list or [])[:3]:
                info = p.get("info") if isinstance(p.get("info"), dict) else p
                out.append({
                    "symbol": info.get("symbol"),
                    "side": info.get("side"),
                    "size": info.get("size"),
                    "positionIdx": info.get("positionIdx"),
                })
            return out

        return {
            "ok": True,
            "symbol": symbol,
            "profile": profile or "main/sub",
            "main_count": len(main_positions),
            "sub_count": len(sub_positions),
            "main_samples": sample(main_positions),
            "sub_samples": sample(sub_positions),
        }
    except Exception as e:
        logger.exception("positions-debug: %s", e)
        return {"ok": False, "error": str(e), "main_count": 0, "sub_count": 0}


@app.get("/api/hedge/current-symbol")
async def api_get_hedge_current_symbol(user: dict = Depends(require_auth)):
    """
    Aktuelles Symbol aus Config (long_config/short_config).
    Beim Login soll das Dashboard immer diesen Coin anzeigen, nicht ein altes URL-Symbol (z.B. DASHUSDT).
    """
    symbol = _get_current_symbol_from_config()
    return {"success": True, "symbol": symbol or ""}


@app.get("/api/hedge/symbols")
async def api_get_hedge_symbols(
    user: dict = Depends(require_auth),
    positions_only: bool = Query(False, description="If true, return only symbols that have an open position (for dropdown); no fallback to logs/config"),
    profile: Optional[str] = Query(None, description="main|bot_1|bot_2 bei positions_only: Symbole direkt von Bybit"),
):
    """Get all symbols with hedge positions. With positions_only=true: only symbols with open position. Bei profile: direkt von Bybit."""
    if positions_only and profile and profile in ("main", "bot_1", "bot_2"):
        try:
            long_key, long_sec, short_key, short_sec = _get_account_keys_by_profile(profile)
            if not all([long_key, long_sec, short_key, short_sec]):
                log_symbols = sorted(set(_list_symbols_from_logs("long") + _list_symbols_from_logs("short") + _list_symbols_from_dropdown_config_sources(profile=profile)))
                return {"success": True, "symbols": log_symbols, "count": len(log_symbols), "hedge_count": len(log_symbols), "debug": {"source": "profile_keys_missing"}}
            long_om = BybitOrderManager(long_key, long_sec)
            short_om = BybitOrderManager(short_key, short_sec)
            long_pos, short_pos = await asyncio.gather(
                asyncio.to_thread(long_om.fetch_positions_direct, None, 5),
                asyncio.to_thread(short_om.fetch_positions_direct, None, 5),
            )
            symbols_set = set()
            for pos_list in (long_pos or [], short_pos or []):
                for p in pos_list:
                    info = p.get("info", {}) or p
                    sym = (info.get("symbol") or p.get("symbol") or "").strip().upper()
                    size = float(info.get("size", 0) or 0)
                    if sym and size > 0:
                        symbols_set.add(sym)
            cfg = _list_symbols_from_symbol_config_files(profile=profile)
            merged = sorted(set(symbols_set) | set(cfg))
            return {"success": True, "symbols": merged, "count": len(merged), "hedge_count": len(symbols_set), "debug": {"source": "profile_bybit", "profile": profile}}
        except Exception as e:
            logger.warning(f"[API] symbols positions_only+profile={profile} Fehler: {e}")
            log_symbols = sorted(s for s in set(_list_symbols_from_logs("long") + _list_symbols_from_logs("short") + _list_symbols_from_dropdown_config_sources(profile=profile)) if s not in SYMBOLS_EXCLUDED_FROM_DROPDOWN_FALLBACK)
            return {"success": True, "symbols": log_symbols, "count": len(log_symbols), "hedge_count": len(log_symbols), "debug": {"source": "profile_fallback", "error": str(e)}}
    if positions_only:
        master_url = f"{MASTER_BOT_API_URL}/master/positions"
        logger.info(f"[API] /api/hedge/symbols?positions_only=true - Master URL: {master_url}")
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    master_url,
                    headers={
                        "X-Request-ID": str(uuid.uuid4()),
                        "X-Internal-Token": MASTER_BOT_API_TOKEN,
                        "Content-Type": "application/json",
                    },
                )
            logger.info(f"[API] /api/hedge/symbols?positions_only=true - HTTP {response.status_code}")
            body_text = response.text
            logger.debug("[API] Master API body (positions_only): %s", body_text[:800])
            if response.status_code != 200:
                fallback = await _fallback_symbols_from_bybit(profile, f"http_status_{response.status_code}")
                if fallback:
                    return fallback
                log_symbols = sorted(set(
                    _list_symbols_from_logs("long") + _list_symbols_from_logs("short") + _list_symbols_from_dropdown_config_sources()
                ))
                return {"success": True, "symbols": log_symbols, "count": len(log_symbols), "hedge_count": len(log_symbols), "debug": {"source": "logs_and_config_fallback"}}
            api_response = response.json()
            if not api_response.get("success"):
                warning = "[API] /api/hedge/symbols?positions_only=true - API success=false, fallback to Bybit"
                logger.warning(warning)
                fallback = await _fallback_symbols_from_bybit(profile, "api_success_false")
                if fallback:
                    return fallback
                log_symbols = sorted(
                    s for s in set(_list_symbols_from_logs("long") + _list_symbols_from_logs("short") + _list_symbols_from_dropdown_config_sources())
                    if s not in SYMBOLS_EXCLUDED_FROM_DROPDOWN_FALLBACK
                )
                return {"success": True, "symbols": log_symbols, "count": len(log_symbols), "hedge_count": len(log_symbols), "debug": {"source": "logs_and_config_fallback"}}
            data = api_response.get("data", {})
            positions_data = data.get("positions", {})
            symbols_with_position = []
            for symbol, positions in positions_data.items():
                if positions.get("long") or positions.get("short"):
                    symbols_with_position.append(symbol)
            cfg_symbols = _list_symbols_from_symbol_config_files()
            merged = sorted(set([s.strip().upper() for s in symbols_with_position] + cfg_symbols))
            logger.info(f"[API] /api/hedge/symbols?positions_only=true - positions={len(symbols_with_position)} config={len(cfg_symbols)} merged={len(merged)}")
            return {
                "success": True,
                "symbols": merged,
                "count": len(merged),
                "hedge_count": len(symbols_with_position),
                "debug": {"source": "positions_only_plus_config", "positions": symbols_with_position, "config": cfg_symbols},
            }
        except Exception as e:
            logger.warning(f"[API] /api/hedge/symbols?positions_only=true - Fehler: {e}, Fallback auf Logs+Config oder Bybit")
            fallback = await _fallback_symbols_from_bybit(profile, "exception")
            if fallback:
                return fallback
            log_symbols = sorted(
                s for s in set(_list_symbols_from_logs("long") + _list_symbols_from_logs("short") + _list_symbols_from_dropdown_config_sources())
                if s not in SYMBOLS_EXCLUDED_FROM_DROPDOWN_FALLBACK
            )
            return {
                "success": True,
                "symbols": log_symbols,
                "count": len(log_symbols),
                "hedge_count": len(log_symbols),
                "debug": {"source": "logs_and_config_fallback", "error": str(e)},
            }

    try:
        logger.info("[API] /api/hedge/symbols - Starte Symbol-Erkennung (via Master Bot API)...")
        
        # Generiere Request-ID für Idempotenz
        request_id = str(uuid.uuid4())
        logger.info(f"📋 Request-ID: {request_id}")
        
        # Rufe Master Bot API auf
        logger.info(f"🌐 Rufe Master Bot API auf: {MASTER_BOT_API_URL}/master/positions")
        
        async with httpx.AsyncClient(timeout=300.0) as client:  # 5 Minuten Timeout für VPN/Bybit API
            try:
                response = await client.get(
                    f"{MASTER_BOT_API_URL}/master/positions",
                    headers={
                        "X-Request-ID": request_id,
                        "X-Internal-Token": MASTER_BOT_API_TOKEN,
                        "Content-Type": "application/json"
                    }
                )
                
                logger.info(f"📥 API Response Status: {response.status_code}")
                
                if response.status_code == 200:
                    api_response = response.json()
                    logger.info(f"📤 API Response erhalten")
                    
                    if api_response.get("success"):
                        data = api_response.get("data", {})
                        all_symbols = data.get("symbols", [])
                        positions_data = data.get("positions", {})
                        
                        logger.info(f"[API] /api/hedge/symbols - Gefundene Symbole: {all_symbols} (Anzahl: {len(all_symbols)})")
                        
                        # Filter symbols that have both Long and Short positions
                        hedge_symbols = []
                        symbols_with_only_long = []
                        symbols_with_only_short = []
                        
                        for symbol in all_symbols:
                            positions = positions_data.get(symbol, {})
                            has_long = positions.get('long', False)
                            has_short = positions.get('short', False)
                            
                            logger.debug(f"[API] /api/hedge/symbols - {symbol}: long={has_long}, short={has_short}")
                            
                            if has_long and has_short:
                                hedge_symbols.append(symbol)
                                logger.info(f"[API] /api/hedge/symbols - {symbol}: Hedge-Position gefunden (Long + Short)")
                            elif has_long:
                                symbols_with_only_long.append(symbol)
                                logger.debug(f"[API] /api/hedge/symbols - {symbol}: Nur Long-Position")
                            elif has_short:
                                symbols_with_only_short.append(symbol)
                                logger.debug(f"[API] /api/hedge/symbols - {symbol}: Nur Short-Position")
                        
                        # Sort alphabetically
                        hedge_symbols.sort()
                        symbols_with_only_long.sort()
                        symbols_with_only_short.sort()
                        
                        # WICHTIG: Wenn keine Hedge-Positionen gefunden wurden, aber einzelne Positionen existieren,
                        # füge diese zur Liste hinzu, damit das Dashboard sie anzeigen kann
                        all_symbols_with_positions = hedge_symbols.copy()
                        if len(hedge_symbols) == 0:
                            # Keine Hedge-Positionen → zeige auch einzelne Positionen an
                            all_symbols_with_positions.extend(symbols_with_only_long)
                            all_symbols_with_positions.extend(symbols_with_only_short)
                            logger.info(f"[API] /api/hedge/symbols - Keine Hedge-Positionen gefunden, zeige {len(all_symbols_with_positions)} einzelne Position(en) an")
                        else:
                            logger.info(f"[API] /api/hedge/symbols - {len(hedge_symbols)} Hedge-Symbole gefunden")
                        
                        logger.info(f"[API] /api/hedge/symbols - Ergebnis: {len(hedge_symbols)} Hedge-Symbole, {len(symbols_with_only_long)} nur Long, {len(symbols_with_only_short)} nur Short, Gesamt für Anzeige: {len(all_symbols_with_positions)}")
                        
                        if all_symbols_with_positions:
                            return {
                                "success": True,
                                "symbols": all_symbols_with_positions,
                                "count": len(all_symbols_with_positions),
                                "hedge_count": len(hedge_symbols),
                                "debug": {
                                    "all_symbols": all_symbols,
                                    "symbols_with_only_long": symbols_with_only_long,
                                    "symbols_with_only_short": symbols_with_only_short,
                                    "hedge_symbols": hedge_symbols
                                }
                            }

                        logger.warning("[API] /api/hedge/symbols - Keine Symbole von Master Bot API, fallback auf Logs + Config")
                        log_symbols = sorted(set(
                            _list_symbols_from_logs("long") + _list_symbols_from_logs("short") + _list_symbols_from_dropdown_config_sources()
                        ))
                        return {
                            "success": True,
                            "symbols": log_symbols,
                            "count": len(log_symbols),
                            "hedge_count": 0,
                            "debug": {"source": "logs_and_config"}
                        }
                    else:
                        # API-Response hat Fehler
                        error_code = api_response.get("error_code", "UNKNOWN")
                        error_message = api_response.get("message", "Unknown error")
                        logger.error(f"❌ Master Bot API Error ({error_code}): {error_message}")
                        logger.warning("[API] /api/hedge/symbols - API Fehler, fallback auf Logs")
                        log_symbols = sorted(set(_list_symbols_from_logs("long") + _list_symbols_from_logs("short")))
                        return {"success": True, "symbols": log_symbols, "count": len(log_symbols), "hedge_count": 0, "debug": {"source": "logs"}}
                else:
                    # HTTP-Status != 200
                    try:
                        error_response = response.json()
                        error_message = error_response.get("message", f"HTTP {response.status_code}")
                        logger.error(f"❌ HTTP Error {response.status_code}: {error_message}")
                        logger.warning("[API] /api/hedge/symbols - HTTP Error, fallback auf Logs + Config")
                        log_symbols = sorted(set(
                            _list_symbols_from_logs("long") + _list_symbols_from_logs("short") + _list_symbols_from_dropdown_config_sources()
                        ))
                        return {"success": True, "symbols": log_symbols, "count": len(log_symbols), "hedge_count": 0, "debug": {"source": "logs_and_config"}}
                    except:
                        logger.error(f"❌ HTTP Error {response.status_code}: {response.text}")
                        logger.warning("[API] /api/hedge/symbols - HTTP Error body, fallback auf Logs + Config")
                        log_symbols = sorted(set(
                            _list_symbols_from_logs("long") + _list_symbols_from_logs("short") + _list_symbols_from_dropdown_config_sources()
                        ))
                        return {"success": True, "symbols": log_symbols, "count": len(log_symbols), "hedge_count": 0, "debug": {"source": "logs_and_config"}}
                        
            except httpx.HTTPError as e:
                logger.error(f"❌ HTTP-Error beim Aufruf der Master Bot API: {e}", exc_info=True)
                logger.warning("[API] /api/hedge/symbols - API nicht erreichbar, fallback auf Logs + Config")
                log_symbols = sorted(set(
                    _list_symbols_from_logs("long") + _list_symbols_from_logs("short") + _list_symbols_from_dropdown_config_sources()
                ))
                return {"success": True, "symbols": log_symbols, "count": len(log_symbols), "hedge_count": 0, "debug": {"source": "logs_and_config"}}
            except httpx.TimeoutException as e:
                logger.error(f"❌ Timeout beim Aufruf der Master Bot API nach 120s: {e}")
                logger.warning("[API] /api/hedge/symbols - API Timeout, fallback auf Logs + Config")
                log_symbols = sorted(set(
                    _list_symbols_from_logs("long") + _list_symbols_from_logs("short") + _list_symbols_from_dropdown_config_sources()
                ))
                return {"success": True, "symbols": log_symbols, "count": len(log_symbols), "hedge_count": 0, "debug": {"source": "logs_and_config"}}
    except Exception as e:
                logger.error(f"❌ Unerwarteter Fehler beim Aufruf der Master Bot API: {e}", exc_info=True)
                logger.warning("[API] /api/hedge/symbols - Unerwarteter Fehler, fallback auf Logs + Config")
                log_symbols = sorted(set(
                    _list_symbols_from_logs("long") + _list_symbols_from_logs("short") + _list_symbols_from_dropdown_config_sources()
                ))
                return {"success": True, "symbols": log_symbols, "count": len(log_symbols), "hedge_count": 0, "debug": {"source": "logs_and_config"}}
                
    except Exception as e:
        logger.error(f"❌ Unerwarteter Fehler: {e}", exc_info=True)
        logger.warning("[API] /api/hedge/symbols - Exception, fallback auf Logs + Config")
        log_symbols = sorted(set(
            _list_symbols_from_logs("long") + _list_symbols_from_logs("short") + _list_symbols_from_dropdown_config_sources()
        ))
        return {"success": True, "symbols": log_symbols, "count": len(log_symbols), "hedge_count": 0, "debug": {"source": "logs_and_config"}}


@app.post("/api/hedge/symbols/archive")
async def api_archive_symbol(
    data: dict = Body(...),
    user: dict = Depends(require_auth),
):
    """Archiviert ein Symbol, sodass es im Dropdown nicht mehr angezeigt wird."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    symbol = (data.get("symbol") or "").strip().upper()
    result = _archive_symbol(symbol)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Archivierung fehlgeschlagen"))
    return result


@app.post("/api/hedge/symbols/unarchive")
async def api_unarchive_symbol(
    data: dict = Body(...),
    user: dict = Depends(require_auth),
):
    """Hebt die Archivierung eines Symbols auf."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    symbol = (data.get("symbol") or "").strip().upper()
    result = _unarchive_symbol(symbol)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Unarchive fehlgeschlagen"))
    return result

@app.get("/api/hedge/bot-status/{symbol}")
async def api_get_hedge_bot_status(
    symbol: str,
    user: dict = Depends(require_auth),
    profile: Optional[str] = Query(None, description="main|bot_N für Profil-spezifische PID-Prüfung"),
):
    """Get bot status for both long and short bots for a symbol"""
    try:
        from utils.bot_monitor import get_bot_status

        prof = _normalize_dashboard_profile(profile, fallback_to_main=False)
        long_bot_entry = resolve_profile_to_bot_record(prof)
        long_bot_name = long_bot_entry["bot_name"] if long_bot_entry else None

        long_status = get_bot_status(symbol, bot_type="long", bot_name=long_bot_name, profile=prof)
        short_status = get_bot_status(symbol, bot_type="short", profile=prof)

        return {
            "success": True,
            "symbol": symbol,
            "long_bot": long_status,
            "short_bot": short_status,
        }
    except Exception as e:
        logger.error(f"Error getting bot status for {symbol}: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "symbol": symbol,
            "long_bot": {"running": False, "status": "stopped"},
            "short_bot": {"running": False, "status": "stopped"},
        }


def _stop_script_bot(symbol: str, bot_type: str, profile: Optional[str] = None) -> bool:
    """
    Stoppt einen script-gestarteten Bot via PID-Datei (data/run).
    Berücksichtigt Profil (bot_1/bot_2) für profil-spezifische PID-Dateien.
    Returns True wenn gestoppt oder nicht gelaufen, False bei Fehler.
    """
    profile_record = None
    try:
        profile_record = resolve_profile_to_bot_record(profile)
    except ValueError:
        profile_record = None

    if profile_record and bot_type == "long":
        script_path = _long_bot_shared_script_path("stop")
        if not _is_executable_script(script_path):
            logger.error("[stop-script-bot] Shared stop script missing: %s", script_path)
            return False
        result = _run_long_bot_script(script_path, profile_record["bot_name"], "stop_long_bot", project_root)
        return bool(result.get("success"))

    safe_symbol = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in str(symbol or "").strip().upper())
    prof_suffix = f"_{profile}" if _is_registry_bot_profile(profile) else ""
    run_dir = project_root / "data" / "run"
    pid_file = run_dir / f"{bot_type}_bot_{safe_symbol}{prof_suffix}.pid"
    lock_file = run_dir / f"{bot_type}_bot_{safe_symbol}{prof_suffix}.lock"
    if not pid_file.exists():
        return True
    try:
        pid_raw = pid_file.read_text(encoding="utf-8").strip()
        if not pid_raw.isdigit():
            pid_file.unlink(missing_ok=True)
            return True
        pid = int(pid_raw)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pid_file.unlink(missing_ok=True)
            return True
        except Exception as e:
            logger.warning(f"[stop-script-bot] SIGTERM für {bot_type}@{symbol} (PID {pid}): {e}")
        for _ in range(25):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
                time.sleep(0.3)
            except Exception:
                pass
        pid_file.unlink(missing_ok=True)
        try:
            lock_file.unlink(missing_ok=True)
        except Exception:
            pass
        logger.info(f"✅ {bot_type.capitalize()} Bot für {symbol} gestoppt (Script/PID)")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Stoppen des {bot_type}-Bots für {symbol}: {e}", exc_info=True)
        return False


def _start_both_bots_via_script_blocking(
    symbol: str,
    long_usdt: Optional[float] = None,
    short_usdt: Optional[float] = None,
    profile: Optional[str] = None,
):
    """
    Startet beide Bots.
    - Für profile=bot_1/bot_2: Nutzt start_long_bot_1/2.sh und start_short_bot_1/2.sh (dedizierte Skripte
      mit korrektem HEDGE_PROFILE und config/bot_1/ – vermeidet falschen Account beim Restart).
    - Für main: start_both_bots.sh (start_long_main + start_short_sub).
    Blocking, für use in run_in_executor.
    """
    _project_root = Path(__file__).resolve().parent.parent
    prof = (profile or "").strip() if profile else None
    if prof not in ("main", "bot_1", "bot_2"):
        prof = None

    results = {
        "long": {"success": False, "message": "", "running": False},
        "short": {"success": False, "message": "", "running": False}
    }
    try:
        if is_bot_running(symbol, bot_type="long", profile=prof) and is_bot_running(symbol, bot_type="short", profile=prof):
            results["long"] = {"success": True, "message": f"Long Bot für {symbol} läuft bereits", "running": True}
            results["short"] = {"success": True, "message": f"Short Bot für {symbol} läuft bereits", "running": True}
            return results

        # Größen aus long_config/short_config falls nicht übergeben
        if long_usdt is None or short_usdt is None:
            long_cfg = load_config(symbol=symbol, bot_type="long", profile=prof) or {}
            short_cfg = load_config(symbol=symbol, bot_type="short", profile=prof) or {}
            long_usdt = long_usdt if long_usdt is not None else float(long_cfg.get("initial_long_usdt", 20))
            short_usdt = short_usdt if short_usdt is not None else float(short_cfg.get("initial_short_usdt", 20))

        start_config_dir = _project_root / "config" / (prof if prof in ("bot_1", "bot_2") else "")
        if not start_config_dir.name:
            start_config_dir = _project_root / "config"
        start_config_dir.mkdir(parents=True, exist_ok=True)
        start_config_path = start_config_dir / "start_config.yaml"
        start_data = {
            "symbol": symbol,
            "long_bot": {"initial_usdt": round(long_usdt, 2)},
            "short_bot": {"initial_usdt": round(short_usdt, 2)}
        }
        with open(start_config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(start_data, f, default_flow_style=False, allow_unicode=True)

        run_env = {**os.environ, "PYTHONPATH": str(_project_root)}

        if prof in ("bot_1", "bot_2"):
            # Dedizierte Skripte – garantieren richtigen Account + config/bot_1/
            long_script = _project_root / f"start_long_bot_{prof.replace('bot_', '')}.sh"
            short_script = _project_root / f"start_short_bot_{prof.replace('bot_', '')}.sh"
            if not long_script.exists() or not short_script.exists():
                results["long"]["message"] = results["short"]["message"] = f"Skripte für {prof} nicht gefunden: {long_script}, {short_script}"
                return results
            # Parallele Starts (--daemon, Symbol/Größe aus start_config.yaml)
            proc_long = subprocess.run(
                [str(long_script), "--daemon"],
                cwd=str(_project_root),
                capture_output=True,
                text=True,
                timeout=20,
                env=run_env,
            )
            proc_short = subprocess.run(
                [str(short_script), "--daemon"],
                cwd=str(_project_root),
                capture_output=True,
                text=True,
                timeout=20,
                env=run_env,
            )
            time.sleep(2)
            long_ok = is_bot_running(symbol, bot_type="long", profile=prof)
            short_ok = is_bot_running(symbol, bot_type="short", profile=prof)
            results["long"] = {
                "success": long_ok,
                "message": f"Long Bot für {symbol} gestartet" if long_ok else (proc_long.stderr or proc_long.stdout or "Long Bot starten fehlgeschlagen"),
                "running": long_ok
            }
            results["short"] = {
                "success": short_ok,
                "message": f"Short Bot für {symbol} gestartet" if short_ok else (proc_short.stderr or proc_short.stdout or "Short Bot starten fehlgeschlagen"),
                "running": short_ok
            }
        else:
            # Main: start_both_bots.sh
            script_path = _project_root / "start_both_bots.sh"
            if not script_path.exists():
                results["long"]["message"] = results["short"]["message"] = f"Skript nicht gefunden: {script_path}"
                return results
            run_env["HEDGE_PROFILE"] = prof or "main"
            cmd = [str(script_path), "--daemon", symbol, str(int(round(long_usdt))), str(int(round(short_usdt)))]
            proc = subprocess.run(
                cmd,
                cwd=str(_project_root),
                capture_output=True,
                text=True,
                timeout=30,
                env=run_env,
            )
            time.sleep(2)
            long_ok = is_bot_running(symbol, bot_type="long", profile=prof)
            short_ok = is_bot_running(symbol, bot_type="short", profile=prof)
            results["long"] = {
                "success": long_ok,
                "message": f"Long Bot für {symbol} erfolgreich gestartet" if long_ok else (proc.stderr or proc.stdout or "Long Bot starten fehlgeschlagen"),
                "running": long_ok
            }
            results["short"] = {
                "success": short_ok,
                "message": f"Short Bot für {symbol} erfolgreich gestartet" if short_ok else (proc.stderr or proc.stdout or "Short Bot starten fehlgeschlagen"),
                "running": short_ok
            }
            if proc.returncode != 0 and (not long_ok or not short_ok):
                err = (proc.stderr or proc.stdout or "").strip()
                if err:
                    results["long"]["message"] = results["short"]["message"] = err
    except subprocess.TimeoutExpired:
        results["long"]["message"] = results["short"]["message"] = "Start-Skript Timeout"
    except Exception as e:
        results["long"]["message"] = results["short"]["message"] = str(e)
        logger.exception(f"Bot-Start für {symbol} (profile={prof}): {e}")
    return results


@app.post("/api/hedge/start-bots/{symbol}")
async def api_start_hedge_bots(
    symbol: str,
    user: dict = Depends(require_auth),
    body: Optional[dict] = Body(None)
):
    """
    Startet beide Bots über start_both_bots.sh (start_long_main.sh + start_short_sub.sh parallel).
    Optional im Body: long_initial_usdt, short_initial_usdt für Größen.
    """
    try:
        # (C) Block if per-symbol configs are missing for either bot.
        sym = (symbol or "").strip().upper()
        profile = (body.get("profile") or "").strip() if body else None
        if profile not in ("main", "bot_1", "bot_2"):
            profile = None
        long_cfg = get_config_path(bot_type="long", symbol=sym, profile=profile)
        short_cfg = get_config_path(bot_type="short", symbol=sym, profile=profile)
        missing = [str(p) for p in (long_cfg, short_cfg) if not p.exists()]
        if missing:
            return {
                "success": False,
                "both_running": False,
                "symbol": sym,
                "error": f"Config fehlt für {sym}: {', '.join(missing)}. Bitte erst im Dashboard speichern/anlegen.",
                "error_code": "MISSING_SYMBOL_CONFIG",
                "missing": missing,
            }
        # Strikte Config-Only-Policy: Größen NUR aus Config, nie aus Request-Body
        long_usdt = None
        short_usdt = None
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,
            _start_both_bots_via_script_blocking,
            symbol,
            long_usdt,
            short_usdt,
            profile,
        )
        all_success = results["long"]["success"] and results["short"]["success"]
        both_running = results["long"]["running"] and results["short"]["running"]
        if not results["long"]["success"] and results["long"].get("message"):
            logger.error(f"❌ Long Bot für {symbol}: {results['long']['message']}")
        else:
            logger.info(f"✅ Long Bot für {symbol}: {results['long'].get('message', '')}")
        if not results["short"]["success"] and results["short"].get("message"):
            logger.error(f"❌ Short Bot für {symbol}: {results['short']['message']}")
        else:
            logger.info(f"✅ Short Bot für {symbol}: {results['short'].get('message', '')}")
        # Hedge Guardian: wenn beide Bots laufen, Guardian für beide Accounts starten (main+sub)
        if both_running:
            _start_hedge_guardian_after_bots_async("both", symbol=sym)

        return {
            "success": all_success,
            "both_running": both_running,
            "symbol": symbol,
            "results": results,
            "message": f"✅ Beide Bots laufen" if both_running else f"⚠️ Einige Bots konnten nicht gestartet werden oder laufen nicht"
        }
    except Exception as e:
        logger.error(f"Error starting bots for {symbol}: {e}", exc_info=True)
        return {
            "success": False,
            "both_running": False,
            "error": str(e),
            "symbol": symbol
        }


@app.post("/api/hedge/stop-bots/{symbol}")
async def api_stop_hedge_bots(
    symbol: str,
    user: dict = Depends(require_auth),
    body: Optional[dict] = Body(None),
):
    """Stop both long and short bots for a symbol. Bei profile bot_1/bot_2: Script-basiert (PID)."""
    try:
        profile = (body.get("profile") or "").strip() if body else None
        if profile not in ("main", "bot_1", "bot_2"):
            profile = None

        results = {
            "long": {"success": False, "message": ""},
            "short": {"success": False, "message": ""}
        }

        if profile and profile in ("bot_1", "bot_2"):
            sym = (symbol or "").strip().upper()
            long_ok = await asyncio.to_thread(_stop_script_bot, sym, "long", profile)
            time.sleep(0.5)
            short_ok = await asyncio.to_thread(_stop_script_bot, sym, "short", profile)
            results["long"] = {"success": long_ok, "message": f"Long Bot für {symbol} gestoppt"}
            results["short"] = {"success": short_ok, "message": f"Short Bot für {symbol} gestoppt"}
        else:
            from bots.master_bot import stop_bot_for_symbol
            try:
                stop_bot_for_symbol(symbol, bot_type="long")
                results["long"] = {"success": True, "message": f"Long Bot für {symbol} gestoppt"}
            except Exception as e:
                results["long"] = {"success": False, "message": str(e)}
            time.sleep(1)
            try:
                stop_bot_for_symbol(symbol, bot_type="short")
                results["short"] = {"success": True, "message": f"Short Bot für {symbol} gestoppt"}
            except Exception as e:
                results["short"] = {"success": False, "message": str(e)}
        
        all_success = results["long"]["success"] and results["short"]["success"]
        # Wenn beide Bots gestoppt: Hedge Guard ebenfalls stoppen (evtl. neuer Coin später)
        if all_success:
            _stop_hedge_guardian("both")
        return {
            "success": all_success,
            "symbol": symbol,
            "results": results,
            "message": f"Bots gestoppt" if all_success else f"Einige Bots konnten nicht gestoppt werden"
        }
    except Exception as e:
        logger.error(f"Error stopping bots for {symbol}: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "symbol": symbol
        }


@app.post("/api/hedge/restart-bots/{symbol}")
async def api_restart_hedge_bots(
    symbol: str,
    user: dict = Depends(require_auth),
    body: Optional[dict] = Body(None),
):
    """Restart both long and short bots for a symbol.
    Bei profile bot_1/bot_2: Script-basiert (Stop via PID + Start via start_both_bots.sh).
    Sonst: systemctl restart/start (Main-Profil)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    profile = (body.get("profile") or "").strip() if body else None
    if profile not in ("main", "bot_1", "bot_2"):
        profile = None

    try:
        results = {
            "long": {"stop": False, "start": False, "message": ""},
            "short": {"stop": False, "start": False, "message": ""}
        }

        if profile and profile in ("bot_1", "bot_2"):
            # Script-basierter Restart für Bot 1/Bot 2
            sym = (symbol or "").strip().upper()
            await asyncio.gather(
                asyncio.to_thread(_stop_script_bot, sym, "long", profile),
                asyncio.to_thread(_stop_script_bot, sym, "short", profile),
            )
            results["long"]["stop"] = results["short"]["stop"] = True
            time.sleep(1)
            loop = asyncio.get_event_loop()
            start_results = await loop.run_in_executor(
                None,
                _start_both_bots_via_script_blocking,
                sym,
                None, None, profile
            )
            results["long"]["start"] = start_results["long"]["success"] or start_results["long"]["running"]
            results["short"]["start"] = start_results["short"]["success"] or start_results["short"]["running"]
            results["long"]["message"] = start_results["long"].get("message", "")
            results["short"]["message"] = start_results["short"].get("message", "")
            success = results["long"]["start"] and results["short"]["start"]
        else:
            # systemctl für Main-Profil
            def _systemctl_is_active(service_name: str) -> bool:
                result = run_sudo_command(
                    ['sudo', 'systemctl', 'is-active', service_name],
                    timeout=5
                )
                return result.returncode == 0 and result.stdout.strip() == 'active'

            def _ensure_service_running(bot_type: str):
                service_name = f'hedgebot-{bot_type}@{symbol}'
                active = _systemctl_is_active(service_name)
                action = 'restart' if active else 'start'
                result = run_sudo_command(
                    ['sudo', 'systemctl', action, service_name],
                    timeout=15
                )
                if result.returncode == 0:
                    logger.info(f"✅ {bot_type.capitalize()} Bot für {symbol} {action}ed")
                    return True, ""
                logger.error(f"❌ Fehler beim {action}en des {bot_type}-Bots für {symbol}: {result.stderr}")
                return False, result.stderr or f"Unknown systemctl {action} error"

            (long_ok, long_msg), (short_ok, short_msg) = await asyncio.gather(
                asyncio.to_thread(_ensure_service_running, "long"),
                asyncio.to_thread(_ensure_service_running, "short"),
            )
            if long_ok:
                results["long"]["stop"] = results["long"]["start"] = True
            else:
                results["long"]["message"] = long_msg
            if short_ok:
                results["short"]["stop"] = results["short"]["start"] = True
            else:
                results["short"]["message"] = short_msg
            success = long_ok and short_ok

        if success:
            _start_hedge_guardian_after_bots_async("both", symbol=str(symbol).strip().upper())

        return {
            "success": success,
            "symbol": symbol,
            "results": results,
            "message": "Bots neu gestartet" if success else "Einige Bots konnten nicht neu gestartet werden"
        }
    except Exception as e:
        logger.error(f"Error restarting bots for {symbol}: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "symbol": symbol
        }


def _apply_set_tp_config(
    symbol: str,
    long_tp_percentage: float,
    short_tp_percentage: float,
    burns_before_rebuy: int = None,
    burn_mode: str = None,
    target: str = "both",
    profile: Optional[str] = None,
):
    """Schreibt TP + Rebuy + optional burn_mode in per-symbol Config(s).

    target: 'long' | 'short' | 'both'
    profile: main (None) | bot_1 | bot_2 – steuert den Config-Unterordner (config/, config/bot_1/, config/bot_2/).
    """
    results = {
        "long": {"success": False, "message": ""},
        "short": {"success": False, "message": ""},
        "config": {"success": False, "message": ""}
    }
    try:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return False, results, "symbol fehlt"

        if burn_mode is not None and burn_mode.strip().lower() not in ("percentage", "fixed_levels"):
            burn_mode = None

        # Aktuelle Modi aus den bestehenden Configs lesen, um in fixed_price nichts zu überschreiben
        prof = (profile or "").strip().lower() or None
        long_cfg = load_config(symbol=symbol, bot_type="long", fallback_to_global=True, profile=prof) or {}
        short_cfg = load_config(symbol=symbol, bot_type="short", fallback_to_global=True, profile=prof) or {}
        long_mode = (long_cfg.get("long_tp_mode") or "percent").strip().lower()
        if long_mode not in ("percent", "fixed_price"):
            long_mode = "percent"
        short_mode = (short_cfg.get("short_tp_mode") or "percent").strip().lower()
        if short_mode not in ("percent", "fixed_price"):
            short_mode = "percent"

        if target in ("long", "both"):
            long_delta = {"symbol": symbol}
            # Nur im Prozent-Modus die Prozentwerte aktualisieren
            if long_mode != "fixed_price":
                long_delta["long_tp_percentage"] = long_tp_percentage
                long_delta["short_tp_percentage"] = short_tp_percentage
            if burns_before_rebuy is not None:
                long_delta["burns_before_rebuy"] = burns_before_rebuy
            if burn_mode is not None:
                long_delta["burn_mode"] = burn_mode.strip().lower()
            # (C) do not auto-create hier; config muss existieren, bevor Bots gestartet werden
            if save_config(symbol=symbol, bot_type="long", config=long_delta, create_if_missing=False, profile=prof):
                results["config"]["long_updated"] = True
            else:
                results["config"]["long_updated"] = False
                cfg_path = get_config_path(bot_type="long", symbol=symbol, profile=prof)
                return False, results, f"Long-Config fehlt oder konnte nicht gespeichert werden: {cfg_path}"

        if target in ("short", "both"):
            short_delta = {"symbol": symbol}
            # Nur im Prozent-Modus die Prozentwerte aktualisieren
            if short_mode != "fixed_price":
                short_delta["short_tp_percentage"] = short_tp_percentage
                short_delta["long_tp_percentage"] = long_tp_percentage
            if burns_before_rebuy is not None:
                short_delta["burns_before_rebuy"] = burns_before_rebuy
            if save_config(symbol=symbol, bot_type="short", config=short_delta, create_if_missing=False, profile=prof):
                results["config"]["short_updated"] = True
            else:
                results["config"]["short_updated"] = False
                cfg_path = get_config_path(bot_type="short", symbol=symbol, profile=prof)
                return False, results, f"Short-Config fehlt oder konnte nicht gespeichert werden: {cfg_path}"

        results["config"]["success"] = True
        msg = f"Long TP = {long_tp_percentage}%, Short TP = {short_tp_percentage}%"
        if burns_before_rebuy is not None:
            msg += f", Rebuy = {burns_before_rebuy}"
        results["config"]["message"] = msg
        log_extra = f", burns_before_rebuy={burns_before_rebuy}" if burns_before_rebuy is not None else ""
        logger.info(f"✅ Config gespeichert (target={target}): long_tp={long_tp_percentage}%, short_tp={short_tp_percentage}%{log_extra}")
        return True, results, results["config"]["message"]
    except Exception as e:
        results["config"]["success"] = False
        results["config"]["message"] = str(e)
        logger.error(f"Error updating config files: {e}", exc_info=True)
        return False, results, str(e)


@app.post("/api/hedge/best-settings-by-range")
async def api_best_settings_by_range(
    user: dict = Depends(require_auth),
    data: dict = Body(...),
):
    """Beste Burn-Einstellungen für eine Range berechnen (Script best_settings_by_range.py). Liefert Kompromiss, Bester Score, Max Profit."""
    try:
        target = (data.get("target") or "long").strip().lower()
        if target not in ("long", "short"):
            raise HTTPException(status_code=400, detail="target muss 'long' oder 'short' sein")
        start_val = data.get("start")
        end_val = data.get("end")
        if start_val is None or end_val is None:
            raise HTTPException(status_code=400, detail="start und end (Preis) sind erforderlich")
        try:
            start_price = float(start_val)
            end_price = float(end_val)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="start und end müssen Zahlen sein")
        if start_price <= 0 or end_price <= 0:
            raise HTTPException(status_code=400, detail="start und end müssen positiv sein")
        min_profit = float(data.get("min_profit") or 0.35)
        long_usdt = float(data.get("initial_long_usdt") or data.get("long") or 100.0)
        short_usdt = float(data.get("initial_short_usdt") or data.get("short") or 50.0)
        step_pct = data.get("step_pct")
        remainder = (data.get("remainder") or "last").strip().lower() if data.get("remainder") else "last"
        if remainder not in ("last", "even"):
            remainder = "last"
        script_path = project_root / "scripts" / "best_settings_by_range.py"
        python_exe = project_root / ".venv" / "bin" / "python"
        if not python_exe.exists():
            python_exe = shutil.which("python3") or "python3"
        else:
            python_exe = str(python_exe)
        if not script_path.exists():
            raise HTTPException(status_code=500, detail="Script best_settings_by_range.py nicht gefunden")
        cmd = [
            python_exe,
            str(script_path),
            "--start", str(start_price),
            "--end", str(end_price),
            "--long", str(long_usdt),
            "--short", str(short_usdt),
            "--min-profit", str(min_profit),
            "--json",
        ]
        if step_pct is not None:
            try:
                step_pct_val = float(step_pct)
                if step_pct_val > 0:
                    cmd.extend(["--step-pct", str(step_pct_val), "--remainder", remainder])
            except (TypeError, ValueError):
                pass
        num_burns_val = data.get("burns_before_rebuy") or data.get("num_burns")
        if num_burns_val is not None:
            try:
                n = int(num_burns_val)
                if 1 <= n <= 20:
                    cmd.extend(["--num-burns", str(n)])
            except (TypeError, ValueError):
                pass
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(project_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace").strip()
            logger.warning(f"[best-settings-by-range] Script exit {proc.returncode}: {err}")
            raise HTTPException(status_code=500, detail=f"Berechnung fehlgeschlagen: {err or 'unbekannter Fehler'}")
        raw = (stdout or b"").decode("utf-8", errors="replace").strip()
        try:
            out = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"[best-settings-by-range] JSON parse error: {e}")
            raise HTTPException(status_code=500, detail="Ungültige Script-Ausgabe")
        return {"success": True, "target": target, "data": out}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error best-settings-by-range: {e}", exc_info=True)
        return {"success": False, "error": str(e), "target": data.get("target", "")}


@app.post("/api/hedge/update-config")
async def api_update_config(
    user: dict = Depends(require_auth),
    data: dict = Body(...)
):
    """Config aktualisieren: nur übergebene Felder schreiben (leere Felder ignoriert).

    target: 'long' | 'short'
    profile (im Body): main | bot_1 | bot_2 – bestimmt den Config-Unterordner.
    """
    try:
        target = (data.get("target") or "").strip().lower()
        if target not in ("long", "short"):
            raise HTTPException(status_code=400, detail="target muss 'long' oder 'short' sein")
        delta = {}
        if data.get("long_tp_percentage") is not None:
            try:
                delta["long_tp_percentage"] = float(data["long_tp_percentage"])
            except (TypeError, ValueError):
                pass
        if data.get("short_tp_percentage") is not None:
            try:
                delta["short_tp_percentage"] = float(data["short_tp_percentage"])
            except (TypeError, ValueError):
                pass
        if data.get("burns_before_rebuy") is not None:
            try:
                v = int(data["burns_before_rebuy"])
                if v >= 1:
                    delta["burns_before_rebuy"] = v
            except (TypeError, ValueError):
                pass
        burn_mode_value = None
        if data.get("burn_mode") is not None:
            bm = (data.get("burn_mode") or "").strip().lower()
            if bm in ("percentage", "fixed_levels", "atr", "dynamic_spread"):
                delta["burn_mode"] = bm
                burn_mode_value = bm
        if data.get("burn_pct") is not None:
            try:
                v = float(data["burn_pct"])
                if 0 < v <= 1:
                    delta["burn_pct"] = v
            except (TypeError, ValueError):
                pass
        if data.get("burn_profit_pct") is not None:
            try:
                v = float(data["burn_profit_pct"])
                if 0 < v <= 1:
                    delta["burn_profit_pct"] = v
            except (TypeError, ValueError):
                pass
        # Burn-Trigger-Distanz in % (separat vom Profit-TP)
        if data.get("burn_distance_percentage") is not None:
            try:
                v = float(str(data.get("burn_distance_percentage")).replace(",", "."))
                if v > 0:
                    delta["burn_distance_percentage"] = v
            except (TypeError, ValueError):
                pass
        # dynamic_spread Parameter
        if data.get("target_net_burn_profit_pct") is not None:
            try:
                v = float(str(data.get("target_net_burn_profit_pct")).replace(",", "."))
                if v >= 0:
                    delta["target_net_burn_profit_pct"] = round(v, 4)
            except (TypeError, ValueError):
                pass
        if data.get("min_burn_distance_pct") is not None:
            try:
                v = float(str(data.get("min_burn_distance_pct")).replace(",", "."))
                if v >= 0:
                    delta["min_burn_distance_pct"] = round(v, 4)
            except (TypeError, ValueError):
                pass
        if data.get("max_burn_distance_pct") is not None:
            try:
                v = float(str(data.get("max_burn_distance_pct")).replace(",", "."))
                if v >= 0:
                    delta["max_burn_distance_pct"] = round(v, 4)
            except (TypeError, ValueError):
                pass
        if data.get("fee_rate") is not None:
            try:
                v = float(str(data.get("fee_rate")).replace(",", "."))
                if v >= 0:
                    delta["fee_rate"] = round(v, 6)
            except (TypeError, ValueError):
                pass
        # ATR-Burn: Flags bleiben für Backwards-Compat, werden aber bei burn_mode='atr' automatisch aktiviert
        if "atr_burn_enabled" in data:
            delta["atr_burn_enabled"] = bool(data.get("atr_burn_enabled"))
        if "burn_atr_enabled" in data:
            # Alias / neue Benennung: burn_atr_enabled
            delta["burn_atr_enabled"] = bool(data.get("burn_atr_enabled"))
        # Auto: wenn Burn-Mode explizit auf ATR gesetzt wurde, Flag(s) automatisch aktivieren
        if burn_mode_value == "atr":
            delta.setdefault("burn_atr_enabled", True)
            # Für ältere Configs, die noch atr_burn_enabled benutzen
            delta.setdefault("atr_burn_enabled", True)
        # ATR-Multipliers/Clamps (Burn + TP) – optional, getrennt
        for key in (
            "burn_atr_multiplier",
            "burn_atr_min_pct",
            "burn_atr_max_pct",
            "tp_atr_multiplier",
            "tp_atr_min_pct",
            "tp_atr_max_pct",
        ):
            if data.get(key) is None:
                continue
            try:
                v = float(str(data.get(key)).replace(",", "."))
                if v > 0:
                    delta[key] = v
            except (TypeError, ValueError):
                pass
        # Optional: burn_count im State zurücksetzen/anpassen (z.B. 0 setzen, um Rebuy-Zyklus neu zu starten)
        new_burn_count: int | None = None
        if data.get("burn_count") is not None:
            try:
                bc = int(data["burn_count"])
                if bc >= 0:
                    new_burn_count = bc
            except (TypeError, ValueError):
                new_burn_count = None
        # TP-Mode (Prozent vs. fester Preis)
        if data.get("long_tp_mode") is not None:
            m = (str(data.get("long_tp_mode")) or "").strip().lower()
            if m in ("percent", "fixed_price", "atr"):
                delta["long_tp_mode"] = m
        if data.get("short_tp_mode") is not None:
            m = (str(data.get("short_tp_mode")) or "").strip().lower()
            if m in ("percent", "fixed_price", "atr"):
                delta["short_tp_mode"] = m
        if data.get("long_tp_fixed_price") is not None:
            try:
                v = float(str(data.get("long_tp_fixed_price")).replace(",", "."))
                if v > 0:
                    delta["long_tp_fixed_price"] = v
            except (TypeError, ValueError):
                pass
        if data.get("short_tp_fixed_price") is not None:
            try:
                v = float(str(data.get("short_tp_fixed_price")).replace(",", "."))
                if v > 0:
                    delta["short_tp_fixed_price"] = v
            except (TypeError, ValueError):
                pass
        if data.get("symbol") is not None and str(data.get("symbol") or "").strip():
            delta["symbol"] = str(data["symbol"]).strip().upper()
        # next_cycle_rebuys: Rebuy-Werte für 2., 3., … Zyklus (neben Preis-Levels)
        if "next_cycle_rebuys" in data and isinstance(data.get("next_cycle_rebuys"), list):
            try:
                rebuys = []
                for x in data["next_cycle_rebuys"]:
                    if x is None:
                        continue
                    try:
                        v = int(x)
                        if v >= 1:
                            rebuys.append(v)
                    except (TypeError, ValueError):
                        pass
                delta["next_cycle_rebuys"] = rebuys
            except Exception as e:
                logger.debug(f"[update-config] next_cycle_rebuys parse: {e}")
        # burn_levels: wie Update-Button, explizit aus Request übernehmen und in Config schreiben
        if "burn_levels" in data and isinstance(data.get("burn_levels"), list):
            try:
                levels = []
                for x in data["burn_levels"]:
                    if x is None:
                        continue
                    if isinstance(x, (int, float)) and not (x != x):
                        levels.append(float(x))
                    elif isinstance(x, str) and (x or "").strip():
                        levels.append(float((x or "").strip().replace(",", ".")))
                delta["burn_levels"] = levels
                logger.debug(f"[update-config] burn_levels übernommen (target={target}): {levels}")
            except (TypeError, ValueError) as e:
                logger.warning(f"[update-config] burn_levels parse error: {e}")

        # exit_levels: Liste von festen Exit-TP Preisen (TP1/TP2/...) – explizit aus Request übernehmen
        if "exit_levels" in data and isinstance(data.get("exit_levels"), list):
            try:
                levels = []
                for x in data["exit_levels"]:
                    if x is None:
                        continue
                    if isinstance(x, (int, float)) and not (x != x):
                        v = float(x)
                    elif isinstance(x, str) and (x or "").strip():
                        v = float((x or "").strip().replace(",", "."))
                    else:
                        continue
                    if v > 0:
                        levels.append(v)
                delta["exit_levels"] = levels
                logger.debug(f"[update-config] exit_levels übernommen (target={target}): {levels}")
            except (TypeError, ValueError) as e:
                logger.warning(f"[update-config] exit_levels parse error: {e}")
        # initial_long_usdt / initial_short_usdt: übernehmen wenn explizit im Request (Formular „Größe“).
        # Bei neuen Configs (Profil bot_1/bot_2) sorgt save_config_with_cycles für Default aus Main-Template.
        if data.get("initial_long_usdt") is not None and target == "long":
            try:
                v = float(str(data["initial_long_usdt"]).replace(",", "."))
                if v > 0:
                    delta["initial_long_usdt"] = round(v, 2)
            except (TypeError, ValueError):
                pass
        if data.get("initial_short_usdt") is not None and target == "short":
            try:
                v = float(str(data["initial_short_usdt"]).replace(",", "."))
                if v > 0:
                    delta["initial_short_usdt"] = round(v, 2)
            except (TypeError, ValueError):
                pass
        if data.get("be_target_profit") is not None:
            try:
                v = float(str(data["be_target_profit"]).replace(",", "."))
                if v >= 0:
                    delta["be_target_profit"] = round(v, 2)
            except (TypeError, ValueError):
                pass
        if "exit_close" in data:
            delta["exit_close"] = bool(data["exit_close"])
        if "next_cycles" in data and isinstance(data.get("next_cycles"), list):
            delta["next_cycles"] = data["next_cycles"]
            logger.info("[update-config] next_cycles übernommen (target=%s), len=%s", target, len(delta["next_cycles"]))
        if "start_price" in data:
            raw = data["start_price"]
            if raw is None or (isinstance(raw, str) and str(raw).strip() == ""):
                delta["start_price"] = None
            else:
                try:
                    v = float(str(raw).strip().replace(",", "."))
                    if v > 0:
                        delta["start_price"] = v
                    else:
                        delta["start_price"] = None
                except (TypeError, ValueError):
                    delta["start_price"] = None
        if not delta and new_burn_count is None:
            return {"success": True, "message": "Keine Felder zum Aktualisieren (leere Felder werden ignoriert).", "target": target}
        if target == "long":
            if "short_tp_percentage" not in delta and "long_tp_percentage" in delta:
                delta["short_tp_percentage"] = delta["long_tp_percentage"]
            elif "long_tp_percentage" not in delta and "short_tp_percentage" in delta:
                delta["long_tp_percentage"] = delta["short_tp_percentage"]
        else:
            if "long_tp_percentage" not in delta and "short_tp_percentage" in delta:
                delta["long_tp_percentage"] = delta["short_tp_percentage"]
            elif "short_tp_percentage" not in delta and "long_tp_percentage" in delta:
                delta["short_tp_percentage"] = delta["long_tp_percentage"]
        symbol = (data.get("symbol") or "").strip().upper()
        if not symbol:
            return {"success": False, "target": target, "message": "symbol fehlt (bitte Coin auswählen).", "error_code": "MISSING_SYMBOL"}

        profile = (data.get("profile") or "").strip().lower() or None

        # Long: Vollconfig laden, Delta anwenden, im Block-Format (Zyklus 1 + next_cycles) speichern.
        if target == "long":
            existing = load_config(symbol=symbol, bot_type="long", fallback_to_global=True, profile=profile) or {}
            existing.update(delta)
            logger.info("[update-config] long: existing nach merge, next_cycles in config: %s", "next_cycles" in existing and len(existing.get("next_cycles", [])))
            ok = save_config_with_cycles(symbol=symbol, config=existing, bot_type="long", create_if_missing=True, profile=profile)
        elif target == "short":
            existing = load_config(symbol=symbol, bot_type="short", fallback_to_global=True, profile=profile) or {}
            existing.update(delta)
            ok = save_config_with_cycles(symbol=symbol, config=existing, bot_type="short", create_if_missing=True, profile=profile)
        else:
            ok = save_config(symbol=symbol, bot_type=target, config=delta, create_if_missing=True, profile=profile)
        if ok:
            logger.info(f"✅ Config aktualisiert (symbol={symbol}, target={target}): {delta}")
            _unarchive_symbol(symbol)
            # State-Datei anpassen, damit die Positionsanzeige (burn_count / burns_before_rebuy) sofort stimmt
            new_burns_before_rebuy = delta.get("burns_before_rebuy")  # int from config merge
            if new_burn_count is not None or new_burns_before_rebuy is not None:
                try:
                    project_root = Path(__file__).resolve().parent.parent
                    state_dir = project_root / "data" / "state"
                    if target == "short":
                        bot_state_file = state_dir / f"short_bot_state_{symbol}.json"
                        account_state_file = state_dir / "account_state_sub.json"
                    else:
                        bot_state_file = state_dir / f"long_bot_state_{symbol}.json"
                        account_state_file = state_dir / "account_state_main.json"

                    bot_state: dict = {}
                    if bot_state_file.exists():
                        try:
                            with bot_state_file.open("r") as f:
                                bot_state = json.load(f) or {}
                        except Exception:
                            bot_state = {}
                    if new_burn_count is not None:
                        bot_state["burn_count"] = new_burn_count
                    if new_burns_before_rebuy is not None:
                        bot_state["burns_before_rebuy"] = new_burns_before_rebuy
                    try:
                        bot_state_file.parent.mkdir(parents=True, exist_ok=True)
                        with bot_state_file.open("w") as f:
                            json.dump(bot_state, f, indent=2)
                    except Exception as e:
                        logger.warning(f"[update-config] Konnte Bot-State {bot_state_file} nicht schreiben: {e}")

                    # Account-State: nur burn_count (nicht burns_before_rebuy)
                    if new_burn_count is not None:
                        if account_state_file.exists():
                            try:
                                with account_state_file.open("r") as f:
                                    acc_state = json.load(f) or {}
                            except Exception:
                                acc_state = {}
                        else:
                            acc_state = {}
                        acc_state["burn_count"] = new_burn_count
                        try:
                            account_state_file.parent.mkdir(parents=True, exist_ok=True)
                            with account_state_file.open("w") as f:
                                json.dump(acc_state, f, indent=2)
                        except Exception as e:
                            logger.warning(f"[update-config] Konnte Account-State {account_state_file} nicht schreiben: {e}")

                    logger.info(
                        "[update-config] State aktualisiert (symbol=%s, target=%s): burn_count=%s, burns_before_rebuy=%s",
                        symbol, target, new_burn_count, new_burns_before_rebuy
                    )
                except Exception as e:
                    logger.warning(f"[update-config] Fehler beim Aktualisieren des States (symbol={symbol}, target={target}): {e}")
        return {"success": ok, "symbol": symbol, "target": target, "message": "Config aktualisiert." if ok else "Fehler beim Speichern."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error update-config: {e}", exc_info=True)
        return {"success": False, "error": str(e), "target": data.get("target", "")}


@app.get("/api/hedge/config-raw")
async def api_get_config_raw(
    user: dict = Depends(require_auth),
    symbol: str = Query(..., description="Symbol, z.B. XRPUSDT"),
    bot_type: str = Query(..., description="long oder short"),
    profile: Optional[str] = Query(None, description="Config-Profil: main | bot_1 | bot_2"),
):
    """Liest die aktuelle Config-Datei als Rohtext (YAML). Für Anzeige und Bearbeitung im Dashboard."""
    try:
        bot_type = (bot_type or "long").strip().lower()
        if bot_type not in ("long", "short"):
            raise HTTPException(status_code=400, detail="bot_type muss 'long' oder 'short' sein")
        sym = (symbol or "").strip().upper()
        if not sym:
            return {"success": False, "error": "symbol fehlt"}
        prof = (profile or "").strip().lower() or None
        path = get_config_path(bot_type=bot_type, symbol=sym, profile=prof)
        if not path.exists():
            expected = get_config_path(bot_type=bot_type, symbol=sym, profile=prof)
            return {"success": False, "error": f"Config-Datei nicht gefunden: {expected}"}
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        # Profil-Titelzeile ergänzen/ersetzen: # Main Long-Config (filename) / # Bot 1 Short-Config (filename) etc.
        filename = path.name
        new_header = get_config_header_comment(prof, bot_type, filename)
        lines = content.splitlines()
        if lines and lines[0].strip().startswith("#") and ("Long-Config" in lines[0] or "Short-Config" in lines[0]):
            lines[0] = new_header.rstrip()
            content = "\n".join(lines) + ("\n" if content.endswith("\n") else "")
        else:
            content = new_header + content
        return {"success": True, "content": content, "path": str(path)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error config-raw GET: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.post("/api/hedge/config-raw")
async def api_save_config_raw(
    user: dict = Depends(require_auth),
    data: dict = Body(...),
):
    """Speichert die Config aus Rohtext (YAML). Validiert YAML, schreibt in die per-Symbol-Datei (inkl. Profil-Unterordner)."""
    try:
        bot_type = (data.get("bot_type") or "long").strip().lower()
        if bot_type not in ("long", "short"):
            raise HTTPException(status_code=400, detail="bot_type muss 'long' oder 'short' sein")
        sym = (data.get("symbol") or "").strip().upper()
        if not sym:
            return {"success": False, "error": "symbol fehlt"}
        content = data.get("content") or ""
        prof = (data.get("profile") or "").strip().lower() or None
        path = get_config_path(bot_type=bot_type, symbol=sym, profile=prof)
        filename = path.name
        # Profil-Titelzeile ergänzen/ersetzen: # Main Long-Config (filename) / # Bot 1 Short-Config (filename) etc.
        new_header = get_config_header_comment(prof, bot_type, filename)
        lines = content.splitlines()
        if lines and lines[0].strip().startswith("#") and ("Long-Config" in lines[0] or "Short-Config" in lines[0]):
            lines[0] = new_header.rstrip()
            content = "\n".join(lines) + ("\n" if content.endswith("\n") else "")
        else:
            content = new_header + content
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as e:
            return {"success": False, "error": f"Ungültiges YAML: {e}"}
        if not isinstance(parsed, dict):
            return {"success": False, "error": "Config muss ein YAML-Objekt (Key/Value) sein."}
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"✅ Config-Rohtext gespeichert: {path}")
        _unarchive_symbol(sym)
        return {"success": True, "message": "Config aktualisiert.", "path": str(path)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error config-raw POST: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.get("/api/hedge/burn-stats")
async def api_get_burn_stats(
    user: dict = Depends(require_auth),
    symbol: str = Query(..., description="Symbol, z.B. XRPUSDT"),
    bot_type: str = Query(..., description="long oder short"),
):
    """
    Liefert geplanten Burn-Plan (Netto-Profit etc.) aus der jeweiligen Bot-State-Datei.
    Wird genutzt, um im Dashboard den erwarteten Burn-Profit bei aktuellem Spread anzuzeigen.
    """
    try:
        bt = (bot_type or "long").strip().lower()
        if bt not in ("long", "short"):
            raise HTTPException(status_code=400, detail="bot_type muss 'long' oder 'short' sein")
        sym = (symbol or "").strip().upper()
        if not sym:
            return {"success": False, "error": "symbol fehlt"}

        stats = get_burn_stats(sym, bt)
        if not stats:
            return {
                "success": False,
                "symbol": sym,
                "bot_type": bt,
                "error": "Kein Burn-Plan im State gefunden (noch kein Burn geplant?).",
            }
        return {"success": True, "symbol": sym, "bot_type": bt, "stats": stats}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error burn-stats: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.get("/api/hedge/burn-sim")
async def api_simulate_burn_profit(
    user: dict = Depends(require_auth),
    symbol: str = Query(..., description="Symbol, z.B. DOGEUSDT"),
    bot_type: str = Query(..., description="long oder short"),
    distance_pct: float | None = Query(None, description="Burn-Distanz in %"),
    burn_price: float | None = Query(None, description="Expliziter Burn-Preis (optional)"),
    profile: str | None = Query(None, description="Profil (main / bot_1 / bot_2)"),
):
    """
    Simuliert den Burn-Profit bei einer festen Distanz (distance_pct in %) auf Basis
    der aktuellen Long/Short-Positionen (Spread).
    """
    try:
        bt = (bot_type or "long").strip().lower()
        if bt not in ("long", "short"):
            raise HTTPException(status_code=400, detail="bot_type muss 'long' oder 'short' sein")
        sym = (symbol or "").strip().upper()
        if not sym:
            return {"success": False, "error": "symbol fehlt"}

        # Preis- oder Distanz-basierte Simulation; die eigentliche Mathe
        # passiert konsistent in simulate_burn_profit() via plan_profit_burn().
        sim = simulate_burn_profit(sym, bt, distance_pct=distance_pct, burn_price=burn_price, profile=profile)
        if not sim:
            return {
                "success": False,
                "symbol": sym,
                "bot_type": bt,
                "error": "Simulation nicht möglich (keine Positionen?).",
            }
        return {"success": True, "symbol": sym, "bot_type": bt, "sim": sim}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error burn-sim: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.get("/api/hedge/tp-sim")
async def api_simulate_tp_profit(
    user: dict = Depends(require_auth),
    symbol: str = Query(..., description="Symbol, z.B. DOGEUSDT"),
    bot_type: str = Query(..., description="long oder short"),
    profit_pct: float | None = Query(None, description="TP-Profit in % des Leg-Notionals"),
    tp_price: float | None = Query(None, description="Exit-Preis (überschreibt profit_pct, wenn gesetzt)"),
):
    """
    Simuliert den Profit in USDT für einen gegebenen TP-Prozentwert
    (bezogen auf die jeweilige Leg-Notional), ohne Hedge-Gegenbein.
    """
    try:
        bt = (bot_type or "long").strip().lower()
        if bt not in ("long", "short"):
            raise HTTPException(status_code=400, detail="bot_type muss 'long' oder 'short' sein")
        sym = (symbol or "").strip().upper()
        if not sym:
            return {"success": False, "error": "symbol fehlt"}

        sim = simulate_tp_profit(sym, bt, profit_pct=profit_pct, tp_price=tp_price)
        if not sim:
            return {
                "success": False,
                "symbol": sym,
                "bot_type": bt,
                "error": "Simulation nicht möglich (keine Positionen?).",
            }
        return {"success": True, "symbol": sym, "bot_type": bt, "sim": sim}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error tp-sim: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.post("/api/hedge/update-burn-levels")
async def api_update_burn_levels(
    user: dict = Depends(require_auth),
    data: dict = Body(...)
):
    """Nur burn_levels in der Config aktualisieren (wie Update-Button, nur für Preis-Levels). target: 'long' | 'short'."""
    try:
        target = (data.get("target") or "").strip().lower()
        if target not in ("long", "short"):
            raise HTTPException(status_code=400, detail="target muss 'long' oder 'short' sein")
        raw = data.get("burn_levels")
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="burn_levels muss eine Liste sein")
        levels = []
        for x in raw:
            if x is None:
                continue
            if isinstance(x, (int, float)) and not (x != x):
                levels.append(float(x))
            elif isinstance(x, str) and (x or "").strip():
                levels.append(float((x or "").strip().replace(",", ".")))
        delta = {"burn_levels": levels}
        symbol = (data.get("symbol") or "").strip().upper()
        if not symbol:
            return {"success": False, "target": target, "message": "symbol fehlt (bitte Coin auswählen).", "error_code": "MISSING_SYMBOL"}
        profile = (data.get("profile") or "").strip().lower() or None
        ok = save_config(symbol=symbol, bot_type=target, config=delta, create_if_missing=True, profile=profile)
        if ok:
            logger.info(f"✅ Burn-Levels aktualisiert (symbol={symbol}, target={target}): {levels}")
        return {"success": ok, "symbol": symbol, "target": target, "message": "Burn-Levels gespeichert." if ok else "Fehler beim Speichern."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error update-burn-levels: {e}", exc_info=True)
        return {"success": False, "error": str(e), "target": data.get("target", "")}


@app.post("/api/hedge/set-tp-config")
async def api_set_hedge_tp_config_body(
    user: dict = Depends(require_auth),
    data: dict = Body(...)
):
    """Config sofort speichern (z. B. aus Dashboard-Watcher). target: 'long' | 'short' | 'both'."""
    try:
        target = (data.get("target") or "both").strip().lower()
        if target not in ("long", "short", "both"):
            target = "both"
        long_tp_percentage = data.get("long_tp_percentage")
        short_tp_percentage = data.get("short_tp_percentage")
        burns_before_rebuy = data.get("burns_before_rebuy")
        burn_mode = data.get("burn_mode")
        symbol = (data.get("symbol") or "").strip().upper()
        if not symbol:
            raise HTTPException(status_code=400, detail="symbol ist erforderlich")
        profile = (data.get("profile") or "").strip().lower() or None

        if long_tp_percentage is None or short_tp_percentage is None:
            raise HTTPException(status_code=400, detail="long_tp_percentage und short_tp_percentage sind erforderlich")

        long_tp_percentage = float(long_tp_percentage)
        short_tp_percentage = float(short_tp_percentage)
        if burns_before_rebuy is not None:
            burns_before_rebuy = int(burns_before_rebuy)
            if burns_before_rebuy < 1:
                burns_before_rebuy = 1
        if burn_mode is not None:
            burn_mode = (burn_mode or "").strip().lower()
            if burn_mode not in ("percentage", "fixed_levels"):
                burn_mode = None

        profile = (data.get("profile") or "").strip().lower() or None
        ok, results, message = _apply_set_tp_config(symbol, long_tp_percentage, short_tp_percentage, burns_before_rebuy, burn_mode=burn_mode, target=target, profile=profile)
        return {"success": ok, "symbol": symbol, "target": target, "results": results, "message": message}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting TP config: {e}", exc_info=True)
        return {"success": False, "error": str(e), "symbol": data.get("symbol", "GLOBAL")}


@app.post("/api/hedge/set-tp-config/{symbol}")
async def api_set_hedge_tp_config(
    symbol: str,
    user: dict = Depends(require_auth),
    data: dict = Body(...)
):
    """Nur Config-Dateien mit TP-Prozentsätzen und optional burns_before_rebuy aktualisieren (VOR Bot-Start)
    
    Diese Funktion aktualisiert NUR die Config-Dateien, setzt KEINE Orders.
    Wird verwendet, um TP-Prozentsätze und Rebuy (burns_before_rebuy) einzustellen, bevor die Bots gestartet werden.
    """
    try:
        long_tp_percentage = data.get("long_tp_percentage")
        short_tp_percentage = data.get("short_tp_percentage")
        burns_before_rebuy = data.get("burns_before_rebuy")
        burn_mode = data.get("burn_mode")
        
        if not long_tp_percentage or not short_tp_percentage:
            raise HTTPException(status_code=400, detail="Long und Short TP Prozentangaben sind erforderlich")
        
        try:
            long_tp_percentage = float(long_tp_percentage)
            short_tp_percentage = float(short_tp_percentage)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Ungültige TP-Prozentangaben")
        if burns_before_rebuy is not None:
            burns_before_rebuy = int(burns_before_rebuy)
            if burns_before_rebuy < 1:
                burns_before_rebuy = 1
        if burn_mode is not None:
            burn_mode = (burn_mode or "").strip().lower()
            if burn_mode not in ("percentage", "fixed_levels"):
                burn_mode = None
        target = (data.get("target") or "both").strip().lower()
        if target not in ("long", "short", "both"):
            target = "both"
        
        profile = (data.get("profile") or "").strip().lower() or None
        ok, apply_results, msg = _apply_set_tp_config(symbol, long_tp_percentage, short_tp_percentage, burns_before_rebuy, burn_mode=burn_mode, target=target, profile=profile)
        results = apply_results
        if not ok:
            results["config"]["message"] = msg
        return {
            "success": ok,
            "symbol": symbol,
            "results": results,
            "message": results["config"].get("message", msg) if ok else msg
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting TP config for {symbol}: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "symbol": symbol
        }


@app.post("/api/hedge/set-tp/{symbol}")
async def api_set_hedge_tps(
    symbol: str,
    user: dict = Depends(require_auth),
    data: dict = Body(...)
):
    """Set Take-Profit orders for both long and short positions and update bot configs
    
    WICHTIG: Prüft, ob beide Bots laufen, bevor Positionen gesetzt werden.
    Wenn Bots nicht laufen, wird ein Fehler zurückgegeben.
    """
    try:
        from bots.master_bot import load_master_config
        from core.bybit_order_manager import BybitOrderManager
        
        # WICHTIG: Prüfe ZUERST, ob beide Bots laufen
        logger.info(f"[SET-TP] Prüfe Bot-Status für {symbol}...")
        long_bot_running = is_bot_running(symbol, bot_type="long")
        short_bot_running = is_bot_running(symbol, bot_type="short")
        
        if not long_bot_running or not short_bot_running:
            missing_bots = []
            if not long_bot_running:
                missing_bots.append("Long-Bot")
            if not short_bot_running:
                missing_bots.append("Short-Bot")
            
            error_msg = f"❌ Bots müssen gestartet sein, bevor Positionen gesetzt werden können. Fehlende Bots: {', '.join(missing_bots)}"
            logger.warning(f"[SET-TP] {error_msg}")
            raise HTTPException(
                status_code=400,
                detail=error_msg
            )
        
        logger.info(f"[SET-TP] ✅ Beide Bots laufen für {symbol} - fahre fort mit TP-Setzung")
        
        long_tp_percentage = data.get("long_tp_percentage")
        short_tp_percentage = data.get("short_tp_percentage")
        update_config = data.get("update_config", True)  # Default: Update config files
        
        if not long_tp_percentage or not short_tp_percentage:
            raise HTTPException(status_code=400, detail="Long und Short TP Prozentangaben sind erforderlich")
        
        long_tp_percentage = float(long_tp_percentage)
        short_tp_percentage = float(short_tp_percentage)
        
        results = {
            "long": {"success": False, "message": ""},
            "short": {"success": False, "message": ""},
            "config": {"success": False, "message": ""}
        }
        
        # Update config files so bots pick up the changes automatically
        if update_config:
            try:
                # Update per-symbol config (A). Do not auto-create hier (C).
                long_config = load_config(symbol=symbol, bot_type="long", fallback_to_global=True) or {}

                long_mode = (long_config.get("long_tp_mode") or "percent").strip().lower()
                if long_mode != "fixed_price":
                    # Nur im Prozent-Modus die Prozent-Werte aktualisieren
                    long_config['long_tp_percentage'] = long_tp_percentage
                    long_config['short_tp_percentage'] = short_tp_percentage
                
                if save_config(symbol=symbol, bot_type="long", config=long_config, create_if_missing=False):
                    results["config"]["long_updated"] = True
                    logger.info(f"✅ long_config_{symbol}.yaml aktualisiert: long_tp_percentage={long_tp_percentage}, short_tp_percentage={short_tp_percentage}")
                else:
                    results["config"]["long_updated"] = False
                    logger.error(f"❌ Fehler beim Speichern der Long-Config für {symbol} (Datei fehlt?)")
                
                # Update short_config_<SYMBOL>.yaml
                short_config = load_config(symbol=symbol, bot_type="short", fallback_to_global=True) or {}
                short_mode = (short_config.get("short_tp_mode") or "percent").strip().lower()
                if short_mode != "fixed_price":
                    # Short-Bot: nur im Prozent-Modus den Prozent-Wert anfassen
                    # (long_tp_percentage in short_config ist der "Burn-TP" für Long-Seite)
                    short_config['long_tp_percentage'] = short_tp_percentage
                
                if save_config(symbol=symbol, bot_type="short", config=short_config, create_if_missing=False):
                    results["config"]["short_updated"] = True
                    logger.info(f"✅ short_config_{symbol}.yaml aktualisiert: long_tp_percentage={short_tp_percentage}")
                else:
                    results["config"]["short_updated"] = False
                    logger.error(f"❌ Fehler beim Speichern der Short-Config für {symbol} (Datei fehlt?)")
                
                results["config"]["success"] = bool(results["config"].get("long_updated")) and bool(results["config"].get("short_updated"))
                results["config"]["message"] = (
                    "Symbol-Config aktualisiert. Bots übernehmen die neuen TPs automatisch."
                    if results["config"]["success"]
                    else "Config-Update fehlgeschlagen (Symbol-Config fehlt?). Bitte erst Config für den Coin speichern."
                )
                
                # Wait a bit for config watcher to detect changes
                import time
                time.sleep(1)
                
            except Exception as e:
                results["config"]["success"] = False
                results["config"]["message"] = f"Fehler beim Aktualisieren der Config: {str(e)}"
                logger.error(f"Error updating config files: {e}", exc_info=True)
        
        # Set TPs directly via Master Bot API (immediate effect)
        logger.info(f"🌐 Rufe Master Bot API auf: {MASTER_BOT_API_URL}/master/set-tp")
        
        # Generiere Request-ID für Idempotenz
        request_id = str(uuid.uuid4())
        logger.info(f"📋 Request-ID: {request_id}")
        
        async with httpx.AsyncClient(timeout=300.0) as client:  # 5 Minuten Timeout für VPN/Bybit API
            try:
                response = await client.post(
                    f"{MASTER_BOT_API_URL}/master/set-tp",
                    json={
                        "symbol": symbol,
                        "long_tp_percentage": long_tp_percentage,
                        "short_tp_percentage": short_tp_percentage
                    },
                    headers={
                        "X-Request-ID": request_id,
                        "X-Internal-Token": MASTER_BOT_API_TOKEN,
                        "Content-Type": "application/json"
                    }
                )
                
                logger.info(f"📥 API Response Status: {response.status_code}")
                
                if response.status_code == 200:
                    api_response = response.json()
                    logger.info(f"📤 API Response erhalten")
                    
                    if api_response.get("success"):
                        # API-Response hat bereits das richtige Format
                        api_data = api_response.get("data", {})
                        api_results = api_data.get("results", {})
                        
                        # Konvertiere API-Response zu Dashboard-Format
                        results["long"] = api_results.get("long", {"success": False, "message": ""})
                        results["short"] = api_results.get("short", {"success": False, "message": ""})
                    else:
                        # API-Response hat Fehler
                        error_code = api_response.get("error_code", "UNKNOWN")
                        error_message = api_response.get("message", "Unknown error")
                        logger.error(f"❌ Master Bot API Error ({error_code}): {error_message}")
                        results["long"] = {"success": False, "message": error_message}
                        results["short"] = {"success": False, "message": error_message}
                else:
                    # HTTP-Status != 200
                    try:
                        error_response = response.json()
                        error_message = error_response.get("message", f"HTTP {response.status_code}")
                        logger.error(f"❌ HTTP Error {response.status_code}: {error_message}")
                        results["long"] = {"success": False, "message": error_message}
                        results["short"] = {"success": False, "message": error_message}
                    except:
                        logger.error(f"❌ HTTP Error {response.status_code}: {response.text}")
                        results["long"] = {"success": False, "message": f"Master Bot API error: {response.text}"}
                        results["short"] = {"success": False, "message": f"Master Bot API error: {response.text}"}
                        
            except httpx.HTTPError as e:
                logger.error(f"❌ HTTP-Error beim Aufruf der Master Bot API: {e}", exc_info=True)
                results["long"] = {"success": False, "message": f"Master Bot API nicht erreichbar: {str(e)}"}
                results["short"] = {"success": False, "message": f"Master Bot API nicht erreichbar: {str(e)}"}
            except httpx.TimeoutException:
                logger.error(f"❌ Timeout beim Aufruf der Master Bot API")
                results["long"] = {"success": False, "message": "Master Bot API Timeout"}
                results["short"] = {"success": False, "message": "Master Bot API Timeout"}
        
        all_success = results["long"]["success"] and results["short"]["success"]
        config_success = results["config"].get("success", False)
        
        return {
            "success": all_success,
            "symbol": symbol,
            "results": results,
            "message": "TPs erfolgreich gesetzt" if all_success else "Einige TPs konnten nicht gesetzt werden",
            "config_updated": config_success
        }
    except Exception as e:
        logger.error(f"Error setting TPs for {symbol}: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "symbol": symbol
        }


@app.get("/api/hedge/equity")
async def api_get_hedge_equity(
    user: dict = Depends(require_auth),
    profile: Optional[str] = Query(None, description="main|bot_1|bot_2 für Account-Auswahl"),
):
    """Get Main and Sub account equity (bzw. Long/Short-Account des Profils)"""
    try:
        available_profiles = {"main", *(profile_data["profile"] for profile_data in get_bot_profiles())}
        if profile and profile in available_profiles and profile != "main":
            main_api_key, main_secret_key, sub_api_key, sub_secret_key = _get_account_keys_by_profile(profile)
        else:
            main_api_key, main_secret_key = _get_account_keys("main")
            sub_api_key, sub_secret_key = _get_account_keys("sub")

        if not any([main_api_key and main_secret_key, sub_api_key and sub_secret_key]):
            return {"success": False, "error": "API-Keys fehlen"}

        import asyncio
        try:
            tasks: list[asyncio.Future] = []
            task_keys: list[tuple[str, str]] = []
            if main_api_key and main_secret_key:
                main_order_manager = BybitOrderManager(main_api_key, main_secret_key)
                tasks.extend([
                    asyncio.to_thread(main_order_manager.get_account_equity),
                    asyncio.to_thread(main_order_manager.get_account_margin_balance),
                    asyncio.to_thread(main_order_manager.get_account_available_balance),
                ])
                task_keys.extend([
                    ("main", "equity"),
                    ("main", "margin"),
                    ("main", "available"),
                ])
            if sub_api_key and sub_secret_key:
                sub_order_manager = BybitOrderManager(sub_api_key, sub_secret_key)
                tasks.extend([
                    asyncio.to_thread(sub_order_manager.get_account_equity),
                    asyncio.to_thread(sub_order_manager.get_account_margin_balance),
                    asyncio.to_thread(sub_order_manager.get_account_available_balance),
                ])
                task_keys.extend([
                    ("sub", "equity"),
                    ("sub", "margin"),
                    ("sub", "available"),
                ])

            values: dict[str, dict[str, Optional[float]]] = {
                "main": {"equity": None, "margin": None, "available": None},
                "sub": {"equity": None, "margin": None, "available": None},
            }
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=30.0
            )
            for result, (side, metric) in zip(results, task_keys):
                if isinstance(result, Exception):
                    logger.error("Fehler beim Abrufen der %s %s: %s", side, metric, result)
                    values[side][metric] = None
                else:
                    values[side][metric] = result
        except asyncio.TimeoutError:
            logger.error("Timeout beim Abrufen der Equity")
            return {"success": False, "error": "API-Aufruf dauerte zu lange (Timeout)"}

        main_equity = values["main"]["equity"]
        main_margin = values["main"]["margin"]
        main_available = values["main"]["available"]
        sub_equity = values["sub"]["equity"]
        sub_margin = values["sub"]["margin"]
        sub_available = values["sub"]["available"]

        if all(value is None for value in (main_equity, main_margin, main_available, sub_equity, sub_margin, sub_available)):
            return {"success": False, "error": "Equity-Daten konnten nicht geladen werden"}

        total_equity = 0
        if main_equity:
            total_equity += main_equity
        if sub_equity:
            total_equity += sub_equity
        total_margin = 0.0
        if main_margin is not None:
            total_margin += main_margin
        if sub_margin is not None:
            total_margin += sub_margin
        total_available = 0.0
        if main_available is not None:
            total_available += main_available
        if sub_available is not None:
            total_available += sub_available
        
        return {
            "success": True,
            "main_equity": round(main_equity, 2) if main_equity else None,
            "sub_equity": round(sub_equity, 2) if sub_equity else None,
            "total_equity": round(total_equity, 2) if total_equity else None,
            "main_margin_balance": round(main_margin, 2) if main_margin is not None else None,
            "sub_margin_balance": round(sub_margin, 2) if sub_margin is not None else None,
            "total_margin_balance": round(total_margin, 2) if total_margin is not None else None,
            "main_available_balance": round(main_available, 2) if main_available is not None else None,
            "sub_available_balance": round(sub_available, 2) if sub_available is not None else None,
            "total_available_balance": round(total_available, 2) if total_available is not None else None,
            "partial": not bool(sub_api_key and sub_secret_key and main_api_key and main_secret_key),
        }
        
    except Exception as e:
        logger.error(f"Fehler beim Abrufen der Equity: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.post("/api/atr/update")
async def api_update_atr_burn(
    symbol: str = Body(..., embed=True),
    timeframe: str = Body("5", embed=True),
    user: dict = Depends(require_auth),
):
    """
    Update ATR-based burn distance for a symbol.

    This uses Bybit's public kline API (no trading keys) and persists the
    result to data/state/atr_burn_<SYMBOL>.json so that bots can pick it up
    on start when ATR-burn mode is enabled.
    """
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="Symbol fehlt")
    tf = (timeframe or "5").strip()
    try:
        state = await asyncio.to_thread(update_atr_burn_state, sym, tf)
        return {
            "success": True,
            "symbol": sym,
            "timeframe": state.timeframe,
            "atr": state.atr,
            "price": state.price,
            "burn_distance_pct": state.burn_distance_pct,
            "updated_at": state.updated_at,
        }
    except Exception as e:
        logger.error(f"[ATR-BURN] API update failed for {sym}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"ATR-Update fehlgeschlagen: {e}")


CLOSED_PNL_ACCOUNTS = tuple(get_closed_pnl_accounts())

TRADE_SUMMARY_EVENTS = (
    "fixed_cycle_last_trade_pnl_persisted",
    "fixed_cycle_trade_pnl_finalized",
)


def _parse_trade_event_line(line: str, pattern: str) -> dict[str, Any] | None:
    payload = line.split(pattern, 1)[1] if pattern in line else ""
    start = payload.find("{")
    if start == -1:
        return None
    json_text = payload[start:]
    try:
        data = ast.literal_eval(json_text)
    except Exception:
        return None
    timestamp_str = line[:23]
    try:
        parsed = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S,%f")
        ts = _normalize_dashboard_datetime(parsed) or datetime.now(timezone.utc)
    except Exception:
        ts = datetime.now(timezone.utc)
    data["__ts"] = ts
    data["__event"] = pattern
    return data


def _collect_trade_events_from_logs(log_paths: list[Path], bot_name: str, profile: str) -> Tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    scanned = []
    for log_path in log_paths:
        if not log_path.exists():
            warnings.append(f"missing log for {bot_name}: {log_path.name}")
            continue
        scanned.append(log_path.name)
        try:
            with log_path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    for pattern in TRADE_SUMMARY_EVENTS:
                        if pattern in line:
                            event = _parse_trade_event_line(line, pattern)
                            if event:
                                event["bot_name"] = bot_name
                                event["profile"] = profile
                                events.append(event)
        except Exception as exc:
            warnings.append(f"failed reading {log_path.name} for {bot_name}: {exc}")
    logger.warning(
        "[profit-summary-scan] profiles=%s bot=%s files=%s events=%s",
        profile,
        bot_name,
        scanned,
        len(events),
    )
    return events, warnings


def _discover_all_long_bots() -> list[str]:
    bots_root = LIVE_BOT_LOGS_ROOT
    if not bots_root.exists():
        return []
    return sorted(
        child.name for child in bots_root.iterdir() if child.is_dir() and child.name.startswith("long_bot_")
    )


def _bot_entry_from_name(bot_name: str) -> dict[str, Any]:
    num = "".join(filter(str.isdigit, bot_name))
    profile = f"bot_{num}" if num else "main"
    return {"bot_name": bot_name, "profile": profile, "bot_dir": str(LIVE_BOT_LOGS_ROOT / bot_name)}


def _gather_trade_log_paths(paths: dict[str, Path | str] | None, bot_name: str) -> list[Path]:
    log_paths: list[Path] = []
    if not paths:
        return log_paths
    base_raw = paths.get("runtime_log_file")
    base = Path(str(base_raw)) if base_raw else None
    if base:
        log_paths.append(base)
        log_paths.extend(sorted(base.parent.glob(f"{base.name}.*")))
        prev = base.with_suffix(base.suffix + ".prev")
        if prev.exists():
            log_paths.append(prev)
    runner_raw = paths.get("runner_stdout_log")
    runner = Path(str(runner_raw)) if runner_raw else None
    if not runner and base:
        runner_candidate = base.parent / "fixed_cycle_runner.stdout.log"
        runner = runner_candidate
    if runner and runner.exists():
        log_paths.append(runner)
    return log_paths


def _build_trade_details(breakdown: dict[str, Any] | None, pnl_source: str | None) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    if not breakdown:
        return details
    for cycle_type, prefix in (
        ("cycle_long_reduce_pnl", "CYCLE_{idx}_LONG_ADD"),
        ("cycle_short_tp_pnl", "CYCLE_{idx}_SHORT_REDUCE"),
    ):
        cycle_map = breakdown.get(cycle_type) or {}
        if isinstance(cycle_map, dict):
            for key, pnl_value in sorted(cycle_map.items(), key=lambda kv: str(kv[0])):
                details.append(
                    {
                        "purpose": prefix.format(idx=key),
                        "pnl": float(pnl_value or 0.0),
                        "source": pnl_source,
                    }
                )
    if final := breakdown.get("final_long_exit_pnl"):
        details.append({"purpose": "LONG_TP_EXIT", "pnl": float(final), "source": pnl_source})
    if final := breakdown.get("final_short_exit_pnl"):
        details.append({"purpose": "SHORT_SL_EXIT", "pnl": float(final), "source": pnl_source})
    return details


SERVER_TZ = timezone(timedelta(hours=2))
DASHBOARD_TZ = timezone(timedelta(hours=3))


def _normalize_dashboard_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_profit_time(value: Any) -> datetime | None:
    normalized = _normalize_dashboard_datetime(value)
    if not normalized:
        return None
    return normalized.astimezone(SERVER_TZ)


def _format_profit_time_label(value: Any) -> str:
    ts = _parse_profit_time(value)
    if not ts:
        return ""
    return ts.astimezone(DASHBOARD_TZ).strftime("%d.%m.%y, %H:%M:%S")


def _get_trade_start_time(record: dict[str, Any]) -> datetime | None:
    detail_times: list[datetime] = []
    for detail in record.get("details") or []:
        for key in ("time", "timestamp", "created_at"):
            if detail.get(key):
                dt = _parse_profit_time(detail.get(key))
                if dt:
                    detail_times.append(dt)
    if detail_times:
        return min(detail_times)
    for key in ("start_time", "created_at", "timestamp", "__ts"):
        if record.get(key):
            dt = _parse_profit_time(record.get(key))
            if dt:
                return dt
    return None


def _get_trade_end_time(record: dict[str, Any]) -> datetime | None:
    if record.get("finalized_at"):
        dt = _parse_profit_time(record.get("finalized_at"))
        if dt:
            return dt
    if record.get("end_time"):
        dt = _parse_profit_time(record.get("end_time"))
        if dt:
            return dt
    detail_times: list[datetime] = []
    for detail in record.get("details") or []:
        for key in ("time", "timestamp", "created_at"):
            if detail.get(key):
                dt = _parse_profit_time(detail.get(key))
                if dt:
                    detail_times.append(dt)
    if detail_times:
        return max(detail_times)
    for key in ("timestamp", "__ts"):
        if record.get(key):
            dt = _parse_profit_time(record.get(key))
            if dt:
                return dt
    return None


def _detail_order_id(detail: dict[str, Any]) -> str:
    for key in ("order_id", "client_order_id", "exchange_order_id"):
        value = detail.get(key)
        if value:
            return str(value)
    return ""


def _build_profit_trade_summary(
    record: dict[str, Any], confirmed_start: datetime | None = None
) -> dict[str, Any]:
    start_dt = _get_trade_start_time(record)
    if confirmed_start and (not start_dt or confirmed_start < start_dt):
        start_dt = confirmed_start
    end_dt = _get_trade_end_time(record)
    return {
        "bot_name": record.get("bot_name"),
        "symbol": record.get("symbol"),
        "trade_block_id": record.get("trade_block_id"),
        "start_time": start_dt.isoformat() if start_dt else None,
        "end_time": end_dt.isoformat() if end_dt else None,
        "start_label": _format_profit_time_label(start_dt),
        "end_label": _format_profit_time_label(end_dt),
        "profit_usdt": record.get("total_trade_pnl"),
        "wallet_after": record.get("wallet_after_trade"),
        "cycle_count": record.get("cycle_count"),
        "status": record.get("status"),
    }


def _build_profit_trade_detail_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    fallback_time = _get_trade_start_time(record) or _get_trade_end_time(record)
    for detail in record.get("details") or []:
        time_val = detail.get("time") or detail.get("timestamp") or detail.get("created_at") or fallback_time
        formatted_time = time_val.isoformat() if isinstance(time_val, datetime) else time_val
        rows.append(
            {
                "time": formatted_time,
                "time_label": _format_profit_time_label(time_val),
                "symbol": record.get("symbol"),
                "order_id": _detail_order_id(detail),
                "pnl_usdt": detail.get("pnl") or detail.get("pnl_usdt") or detail.get("realized_pnl") or 0.0,
                "wallet_after": detail.get("wallet_after") or detail.get("wallet_after_trade") or record.get("wallet_after_trade"),
                "purpose": detail.get("purpose"),
            }
        )
    return rows


def _build_confirmed_detail_rows(record: dict[str, Any], confirmed_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in confirmed_rows:
        rows.append(
            {
                "time": entry.get("timestamp"),
                "time_label": _format_profit_time_label(entry.get("timestamp")),
                "symbol": entry.get("symbol") or record.get("symbol"),
                "order_id": entry.get("exchange_order_id") or entry.get("client_order_id") or "",
                "pnl_usdt": entry.get("closed_pnl") or 0.0,
                "wallet_after": None,
                "purpose": entry.get("purpose"),
            }
        )
    return rows


def _collect_history_paths_for_profile(profile: str) -> list[Path]:
    normalized = _normalize_dashboard_profile(profile, fallback_to_main=False)
    if not normalized or normalized == "main":
        return [DASHBOARD_CLOSED_PNL_HISTORY_FILE, CONFIRMED_ORDER_PNL_HISTORY_FILE]
    entries = []
    all_profiles = get_bot_profiles()
    if normalized == "bot_1":
        entries = all_profiles.copy()
        existing = {entry["bot_name"] for entry in entries}
        for bot_name in _discover_all_long_bots():
            if bot_name not in existing:
                entries.append(_bot_entry_from_name(bot_name))
    else:
        entries = [entry for entry in all_profiles if entry["profile"] == normalized]
    paths: list[Path] = [DASHBOARD_CLOSED_PNL_HISTORY_FILE, CONFIRMED_ORDER_PNL_HISTORY_FILE]
    for entry in entries:
        paths_dict = get_bot_paths(entry.get("bot_name"))
        if not paths_dict:
            continue
        dashboard_path = paths_dict.get("dashboard_closed_pnl_history_file")
        confirmed_path = paths_dict.get("confirmed_order_pnl_history_file")
        if dashboard_path:
            paths.append(dashboard_path)
        if confirmed_path:
            paths.append(confirmed_path)
    return paths


def _collect_confirmed_history_paths(profile: str) -> list[Path]:
    resolved = _normalize_dashboard_profile(profile, fallback_to_main=False)
    paths = []
    if not resolved or resolved == "main":
        paths.extend([CONFIRMED_ORDER_PNL_HISTORY_FILE])
        return paths
    account_map = _build_dynamic_bot_state_files()
    for bot_name in map(_bot_entry_from_name, _discover_all_long_bots()):
        bot_paths = get_bot_paths(bot_name["bot_name"])
        if bot_paths:
            confirmed = bot_paths.get("confirmed_order_pnl_history_file")
            if confirmed:
                paths.append(confirmed)
    paths.append(CONFIRMED_ORDER_PNL_HISTORY_FILE)
    return paths


def _collect_confirmed_order_pnl_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    rows.append(payload)
        except Exception:
            continue
    return rows


def _collect_confirmed_trade_start_times(profile: str) -> dict[str, datetime]:
    paths = _collect_confirmed_history_paths(profile)
    rows = _collect_confirmed_order_pnl_rows(paths)
    start_times: dict[str, datetime] = {}
    for row in rows:
        tbid = str(row.get("trade_block_id") or "").strip()
        if not tbid:
            continue
        ts = _normalize_dashboard_datetime(row.get("timestamp"))
        if not ts:
            continue
        existing = start_times.get(tbid)
        if not existing or ts < existing:
            start_times[tbid] = ts
    return start_times


def _filter_confirmed_rows(rows: list[dict[str, Any]], trade_block_id: str) -> list[dict[str, Any]]:
    filtered = [row for row in rows if row.get("trade_block_id") == trade_block_id]
    if not filtered:
        return []
    seen: set[str] = set()
    deduped = []
    for row in filtered:
        key = f"{row.get('exchange_order_id') or ''}-{row.get('purpose') or ''}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    deduped.sort(key=lambda entry: _parse_profit_time(entry.get("timestamp")) or datetime.min)
    return deduped


def _load_confirmed_order_pnl_rows_for_trade(
    profile: str, trade_block_id: str, bot_name: str | None = None, symbol: str | None = None
) -> list[dict[str, Any]]:
    paths = _collect_confirmed_history_paths(profile)
    rows = _collect_confirmed_order_pnl_rows(paths)
    candidate = _filter_confirmed_rows(rows, trade_block_id)
    if symbol:
        candidate = [row for row in candidate if str(row.get("symbol") or "").upper() == symbol.upper()] or candidate
    if bot_name:
        candidate = [
            row for row in candidate if str(row.get("bot_name") or "").lower() == bot_name.lower()
        ] or candidate
    return candidate


def _load_history_entries_from_paths(paths: list[Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except Exception:
                        logger.warning("[dashboard] history_line_parse_failed", {"line": line})
                        continue
                    entries.append(payload)
        except Exception:
            logger.warning("[dashboard] history_file_read_failed", {"path": str(path)}, exc_info=True)
    return entries


def _load_history_trade_events_for_profile(profile: str) -> list[dict[str, Any]]:
    entries = _load_history_entries_from_paths(_collect_history_paths_for_profile(profile))
    events: list[dict[str, Any]] = []
    for entry in entries:
        tbid = entry.get("trade_block_id") or entry.get("trade_block") or entry.get("tradeId")
        if not tbid:
            continue
        record = dict(entry)
        ts_val = record.get("finalized_at") or record.get("timestamp") or record.get("__ts") or record.get("created_at")
        record["__ts"] = _normalize_dashboard_datetime(ts_val) or datetime.now(timezone.utc)
        record["__event"] = record.get("__event") or record.get("event") or "dashboard_closed_pnl_history"
        events.append(record)
    return events


def _find_closed_pnl_history_entry(
    profile: str,
    trade_block_id: str,
    bot_name: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any] | None:
    paths = _collect_history_paths_for_profile(profile)
    best: dict[str, Any] | None = None
    best_ts: datetime | None = None
    for entry in _load_history_entries_from_paths(paths):
        if entry.get("trade_block_id") != trade_block_id:
            continue
        if bot_name and str(entry.get("bot_name") or "").lower() != bot_name.lower():
            continue
        if symbol and str(entry.get("symbol") or "").upper() != symbol.upper():
            continue
        ts_val = entry.get("finalized_at") or entry.get("timestamp") or entry.get("__ts")
        ts = _parse_profit_time(ts_val)
        if best is None or (ts and best_ts and ts > best_ts):
            best = entry
            best_ts = ts
        elif best is None:
            best = entry
    return best


def _merge_trade_history_breakdown(record: dict[str, Any], history_entry: dict[str, Any] | None) -> dict[str, Any]:
    if not history_entry:
        return record
    if not record.get("breakdown") and history_entry.get("breakdown"):
        record["breakdown"] = history_entry["breakdown"]
    record.setdefault("pnl_source", record.get("pnl_source") or history_entry.get("source"))
    record["total_trade_pnl"] = record.get("total_trade_pnl") or history_entry.get("total_trade_pnl")
    record["finalized_at"] = record.get("finalized_at") or history_entry.get("finalized_at")
    record["details"] = _build_trade_details(record.get("breakdown"), record.get("pnl_source"))
    return record


def _normalize_trade_record(event: dict[str, Any]) -> dict[str, Any]:
    breakdown = event.get("breakdown") or {}
    total_trade_pnl = float(event.get("total_trade_pnl") or event.get("total_trade_profit_usdt") or 0.0)
    cycle_net = float(breakdown.get("cycle_net_pnl") or event.get("cycle_net_pnl") or 0.0)
    final_exit_net = float(breakdown.get("final_exit_net_pnl") or event.get("final_exit_net_pnl") or 0.0)
    final_long = float(breakdown.get("final_long_exit_pnl") or event.get("final_long_exit_pnl") or 0.0)
    final_short = float(breakdown.get("final_short_exit_pnl") or event.get("final_short_exit_pnl") or 0.0)
    cycle_count = (
        len(breakdown.get("cycle_long_reduce_pnl") or {})
        if isinstance(breakdown.get("cycle_long_reduce_pnl"), dict)
        else event.get("cycle_count") or 0
    )
    pnl_source = event.get("pnl_source") or event.get("source") or "runtime_event"
    closed_flags = bool(event.get("pnl_complete")) or bool(event.get("last_trade_pnl_complete"))
    if event.get("finalized_at"):
        closed_flags = True
    if str(event.get("__event") or "").strip() in {
        "fixed_cycle_trade_pnl_finalized",
        "fixed_cycle_last_trade_pnl_persisted",
    }:
        closed_flags = True
    if event.get("total_trade_pnl") is not None and event.get("trade_block_id"):
        closed_flags = True
    status = "closed" if closed_flags else "open"
    return {
        "end_time": event.get("finalized_at") or event.get("timestamp") or event["__ts"].isoformat(),
        "bot_name": event.get("bot_name"),
        "symbol": event.get("symbol"),
        "trade_block_id": event.get("trade_block_id"),
        "short_trade_id": event.get("trade_block_id"),
        "total_trade_pnl": total_trade_pnl,
        "cycle_net_pnl": cycle_net,
        "final_exit_net_pnl": final_exit_net,
        "final_long_exit_pnl": final_long,
        "final_short_exit_pnl": final_short,
        "cycle_count": cycle_count,
        "pnl_source": pnl_source,
        "status": status,
        "details": _build_trade_details(breakdown, pnl_source),
        "wallet_after_trade": event.get("wallet_after_trade") or event.get("wallet_after"),
        "start_wallet": event.get("start_wallet"),
        "start_time": event.get("start_time"),
        "finalized_at": event.get("finalized_at"),
        "timestamp": event.get("timestamp"),
        "created_at": event.get("created_at"),
        "__ts": event.get("__ts"),
    }


def _event_priority(event: dict[str, Any]) -> tuple[int, int, datetime]:
    pnl_complete = 1 if bool(event.get("pnl_complete")) else 0
    is_persisted = 1 if event.get("__event") == "fixed_cycle_last_trade_pnl_persisted" else 0
    ts_val = event.get("finalized_at") or event.get("timestamp")
    parsed = _normalize_dashboard_datetime(ts_val)
    if not parsed:
        parsed = event.get("__ts")
    if not parsed:
        parsed = datetime.now(timezone.utc)
    return (pnl_complete, is_persisted, parsed)


def _resolve_profile_entries(profile: str) -> tuple[list[dict[str, Any]], list[str]]:
    normalized = _normalize_dashboard_profile(profile, fallback_to_main=False)
    if not normalized or normalized == "main":
        return [], [f"unknown profile: {profile}"]
    entries: list[dict[str, Any]] = []
    if normalized == "bot_1":
        entries = get_bot_profiles().copy()
        existing = {entry["bot_name"] for entry in entries}
        for bot_name in _discover_all_long_bots():
            if bot_name not in existing:
                entries.append(_bot_entry_from_name(bot_name))
    else:
        entries = [entry for entry in get_bot_profiles() if entry.get("profile") == normalized]
    if not entries:
        return [], [f"profile has no bots configured: {profile}"]
    return entries, []


def _load_trade_blocks_for_profile(profile: str, limit: int) -> Tuple[list[dict[str, Any]], list[str]]:
    entries, entry_warnings = _resolve_profile_entries(profile)
    if not entries:
        return [], entry_warnings
    warnings: list[str] = entry_warnings.copy()
    events: list[dict[str, Any]] = []
    normalized_profile = _normalize_dashboard_profile(profile, fallback_to_main=False)
    for entry in entries:
        paths = get_bot_paths(entry["bot_name"])
        if not paths:
            bot_dir = LIVE_BOT_LOGS_ROOT / entry["bot_name"]
            warnings.append(f"missing paths for bot {entry['bot_name']}, using fallback dirs")
            paths = {
                "runtime_log_file": bot_dir / "logs" / "fixed_cycle_hedge_runtime.log",
                "runner_stdout_log": bot_dir / "logs" / "fixed_cycle_runner.stdout.log",
            }
        log_paths = _gather_trade_log_paths(paths, entry["bot_name"])
        parsed, bot_warnings = _collect_trade_events_from_logs(log_paths, entry["bot_name"], normalized_profile)
        warnings.extend(bot_warnings)
        events.extend(parsed)
        logger.warning(
            "[profit-summary-scan] profile=%s bot=%s files=%s events=%s",
            profile,
            entry["bot_name"],
            [str(p) for p in log_paths],
            len(parsed),
        )
    history_events = _load_history_trade_events_for_profile(profile)
    if history_events:
        warnings.append(f"loaded {len(history_events)} history events for profile {profile}")
    events.extend(history_events)

    dedup: dict[str, dict[str, Any]] = {}
    priority_map: dict[str, tuple[int, int, datetime]] = {}
    for event in events:
        key = str(event.get("trade_block_id") or "")
        if not key:
            continue
        existing_priority = priority_map.get(key)
        current_priority = _event_priority(event)
        if not existing_priority or current_priority > existing_priority:
            dedup[key] = event
            priority_map[key] = current_priority
    sorted_events = sorted(
        dedup.values(),
        key=lambda evt: evt.get("__ts") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    normalized_trades = [_normalize_trade_record(evt) for evt in sorted_events[:limit]]
    logger.debug(
        "[dashboard] profit_summary_dedup_result",
        {
            "profile": profile,
            "bot_count": len(entries),
            "raw_events": len(events),
            "deduped_trades": len(normalized_trades),
        },
    )
    return normalized_trades, warnings


def _bot_paths_with_fallback(entry: dict[str, Any]) -> dict[str, Path]:
    bot_name = entry.get("bot_name")
    if not bot_name:
        return {}
    base = get_bot_paths(bot_name) or {}
    bot_dir = Path(entry.get("bot_dir") or LIVE_BOT_LOGS_ROOT / bot_name)
    result: dict[str, Path] = {}

    def _resolve(key: str, suffix: str) -> None:
        value = base.get(key)
        if value:
            result[key] = Path(value)
        else:
            result[key] = bot_dir / suffix

    _resolve("state_file", "state/fixed_cycle_state.json")
    _resolve("status_file", "run/status.json")
    _resolve("confirmed_order_pnl_history_file", "logs/confirmed_order_pnl_history.jsonl")
    return result


def _is_bot_running(status: dict[str, Any] | None) -> bool:
    if not status:
        return False
    return str(status.get("status") or "").strip().lower() == "running"


def _normalize_active_orders(payload: dict[str, Any], strategy_state: dict[str, Any]) -> list:
    cycle_state = strategy_state.get("cycle_state") or {}
    orders = []
    for source in (payload, strategy_state, cycle_state):
        if not isinstance(source, dict):
            continue
        for key in ("active_orders", "orders"):
            raw = source.get(key)
            if isinstance(raw, dict):
                orders.extend(raw.values())
            elif isinstance(raw, list):
                orders.extend(raw)
    return [order for order in orders if order]


def _safe_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _has_active_trade_state(state: dict[str, Any], strategy_state: dict[str, Any]) -> bool:
    long_qty = _safe_wallet_float(state.get("long_qty") or strategy_state.get("long_qty"))
    short_qty = _safe_wallet_float(state.get("short_qty") or strategy_state.get("short_qty"))
    if (long_qty or 0.0) > 0.0 or (short_qty or 0.0) > 0.0:
        return True
    if _normalize_active_orders(state, strategy_state):
        return True
    if bool(state.get("initial_entry_submitted")) or bool(strategy_state.get("initial_entry_submitted")):
        return True
    cycle_state = strategy_state.get("cycle_state") or {}
    if bool(strategy_state.get("trade_active")) or bool(cycle_state.get("trade_active")):
        return True
    if _safe_int(strategy_state.get("cycle_completed_count")) > 0 or _safe_int(state.get("cycle_completed_count")) > 0:
        return True
    return False


def _collect_process_rows_for_bot(
    profile: str, bot_name: str, paths: dict[str, Path]
) -> tuple[dict[str, Any] | None, str | None]:
    state = _load_json_file(paths.get("state_file"))
    if not state:
        return None, f"state file missing or unreadable for bot {bot_name}"
    status = _load_json_file(paths.get("status_file"))
    if not _is_bot_running(status):
        return None, None
    strategy_state = state.get("strategy_state") or {}
    trade_block_id = str(
        state.get("trade_block_id") or strategy_state.get("trade_block_id") or strategy_state.get("last_trade_block_id") or ""
    ).strip()
    if not trade_block_id:
        return None, f"trade_block_id missing for running bot {bot_name}"
    if not _has_active_trade_state(state, strategy_state):
        return None, None
    symbol = (
        state.get("symbol")
        or strategy_state.get("symbol")
        or (strategy_state.get("cycle_state") or {}).get("symbol")
        or ""
    )
    rows = _load_confirmed_order_pnl_rows_for_trade(profile, trade_block_id, bot_name, symbol)
    filled_orders: list[dict[str, Any]] = []
    start_times: list[datetime] = []
    total_pnl = 0.0
    max_cycle = 0
    seen_dedupe: set[str] = set()
    for row in rows:
        dedupe_key = row.get("dedupe_key")
        if not dedupe_key:
            dedupe_key = f"{row.get('exchange_order_id') or ''}:{row.get('purpose') or ''}"
        key = str(dedupe_key)
        if key in seen_dedupe:
            continue
        seen_dedupe.add(key)
        ts = _normalize_dashboard_datetime(row.get("timestamp"))
        if ts:
            start_times.append(ts)
        pnl_value = row.get("closed_pnl") or 0.0
        try:
            total_pnl += float(pnl_value)
        except (TypeError, ValueError):
            pass
        cycle_idx = _safe_int(row.get("cycle_index"))
        if cycle_idx > max_cycle:
            max_cycle = cycle_idx
        filled_orders.append(
            {
                "time": row.get("timestamp"),
                "time_label": _format_profit_time_label(row.get("timestamp")),
                "purpose": row.get("purpose") or row.get("trade_type"),
                "cycle_index": row.get("cycle_index"),
                "pnl": pnl_value,
                "scope": row.get("pnl_scope"),
                "symbol": row.get("symbol") or symbol,
                "trade_block_id": trade_block_id,
                "order_id": row.get("exchange_order_id") or row.get("client_order_id"),
            }
        )
    start_dt = min(start_times) if start_times else None
    cycle_completed = max(_safe_int(strategy_state.get("cycle_completed_count")), max_cycle)
    return (
        {
            "bot_name": bot_name,
            "symbol": symbol or "",
            "trade_block_id": trade_block_id,
            "start_time": start_dt.isoformat() if start_dt else None,
            "start_label": _format_profit_time_label(start_dt) if start_dt else "-",
            "end_label": "-",
            "end_time": None,
            "total_trade_pnl": round(total_pnl, 8),
            "wallet_after": None,
            "cycle_count": cycle_completed,
            "status": "in_progress",
            "is_process": True,
            "filled_orders": filled_orders,
            "active_orders": _normalize_active_orders(state, strategy_state),
            "open_orders": strategy_state.get("open_orders"),
            "orders": strategy_state.get("orders"),
        },
        None,
    )


def _collect_active_bot_process_rows(profile: str) -> tuple[list[dict[str, Any]], list[str]]:
    entries, entry_warnings = _resolve_profile_entries(profile)
    warnings: list[str] = entry_warnings.copy()
    rows: list[dict[str, Any]] = []
    for entry in entries:
        bot_name = entry.get("bot_name")
        if not bot_name:
            continue
        paths = _bot_paths_with_fallback(entry)
        row, warning = _collect_process_rows_for_bot(profile, bot_name, paths)
        if warning:
            warnings.append(warning)
        if row:
            rows.append(row)
    return rows, warnings


def _score_trade_record_end_time(record: dict[str, Any]) -> datetime:
    return _get_trade_end_time(record) or datetime.min


@app.get("/api/dashboard/profit-trades")
async def api_profit_trades(
    profile: str = Query("bot_1", description="Bot profile (e.g. bot_1)"),
    limit: int = Query(50, ge=1, le=500),
    user: dict = Depends(require_auth),
):
    trades, warnings = _load_trade_blocks_for_profile(profile, limit)
    normalized = [_normalize_trade_record(evt) for evt in trades]
    normalized.sort(key=_score_trade_record_end_time, reverse=True)
    process_rows, process_warnings = _collect_active_bot_process_rows(profile)
    warnings.extend(process_warnings)
    running_ids = {
        row.get("trade_block_id") for row in process_rows if row.get("trade_block_id")
    }
    filtered_normalized = [
        record for record in normalized if record.get("trade_block_id") not in running_ids
    ]
    closed_records = [record for record in filtered_normalized if record.get("status") == "closed"]
    # _persist_profit_trade_closed_rows(profile, closed_records)
    summary = _summarize_trade_blocks(closed_records)
    confirmed_start_times = _collect_confirmed_trade_start_times(profile)
    summaries = [
        _build_profit_trade_summary(
            record, confirmed_start_times.get(str(record.get("trade_block_id") or ""))
        )
        for record in filtered_normalized[:limit]
    ]
    existing_ids = {
        record.get("trade_block_id") for record in filtered_normalized if record.get("trade_block_id")
    }
    process_rows.sort(
        key=lambda row: _normalize_dashboard_datetime(row.get("start_time"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    process_summaries = []
    for row in process_rows:
        tbid = row.get("trade_block_id")
        if tbid and tbid in existing_ids:
            continue
        process_summaries.append(row)
    summary["open_trades"] = len(process_summaries)
    combined = summaries + process_summaries
    return {
        "profile": profile,
        "summary": summary,
        "count": len(combined),
        "trades": combined,
        "warnings": warnings,
    }


@app.get("/api/dashboard/profit-trades/{trade_block_id}/details")
async def api_profit_trade_details(
    trade_block_id: str,
    profile: str = Query("bot_1", description="Bot profile"),
    user: dict = Depends(require_auth),
):
    trades, warnings = _load_trade_blocks_for_profile(profile, limit=500)
    normalized = [_normalize_trade_record(evt) for evt in trades]
    match = next((record for record in normalized if record.get("trade_block_id") == trade_block_id), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"trade {trade_block_id} not found for profile {profile}")
    bot_name = match.get("bot_name") or ""
    symbol = match.get("symbol") or ""
    confirmed_rows = _load_confirmed_order_pnl_rows_for_trade(profile, trade_block_id, bot_name, symbol)
    if confirmed_rows:
        rows = _build_confirmed_detail_rows(match, confirmed_rows)
    else:
        if not match.get("details"):
            history_entry = _find_closed_pnl_history_entry(profile, trade_block_id, bot_name, symbol)
            match = _merge_trade_history_breakdown(match, history_entry)
        rows = _build_profit_trade_detail_rows(match)
    return {
        "profile": profile,
        "trade_block_id": trade_block_id,
        "bot_name": match.get("bot_name"),
        "symbol": match.get("symbol"),
        "profit_usdt": match.get("total_trade_pnl"),
        "wallet_after": match.get("wallet_after_trade"),
        "rows": rows,
        "warnings": warnings,
    }


def _summarize_trade_blocks(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    trades = list(trades)
    closed_trades = sum(1 for trade in trades if trade.get("status") == "closed")
    closed_records = [trade for trade in trades if trade.get("status") == "closed"]
    total_profit = sum(float(trade.get("total_trade_pnl") or 0.0) for trade in closed_records)
    winning = sum(
        1 for trade in closed_records if float(trade.get("total_trade_pnl") or 0.0) > 0
    )
    losing = sum(
        1 for trade in closed_records if float(trade.get("total_trade_pnl") or 0.0) <= 0
    )
    winrate = (winning / closed_trades * 100.0) if closed_trades else 0.0
    best_bot = None
    best_profit = None
    for trade in closed_records:
        pnl = float(trade.get("total_trade_pnl") or 0.0)
        bot = trade.get("bot_name")
        if best_profit is None or pnl > best_profit:
            best_profit = pnl
            best_bot = bot
    open_trades = sum(1 for trade in trades if trade.get("status") != "closed")
    return {
        "total_profit": round(total_profit, 8),
        "closed_trades": closed_trades,
        "winning_trades": winning,
        "losing_trades": losing,
        "winrate": round(winrate, 2),
        "best_bot": best_bot,
        "open_trades": open_trades,
    }


def _load_persisted_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    logger.warning("[dashboard] dashboard_profit_trade_persist_skip_invalid_line %s", path)
                    continue
                bot = str(data.get("bot_name") or "").lower()
                symbol = str(data.get("symbol") or "").upper()
                tbid = str(data.get("trade_block_id") or "")
                if bot and symbol and tbid:
                    keys.add(f"{bot}|{symbol}|{tbid}")
    except Exception as exc:
        logger.warning("[dashboard] dashboard_profit_trade_persist_failed_read %s %s", path, exc)
    return keys


def _write_jsonl(path: Path, payload: dict[str, Any]) -> None:
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
            fh.write("\n")
    except Exception as exc:
        raise


def _persist_profit_trade_closed_rows(profile: str, closed_rows: list[dict[str, Any]]) -> None:
    if not closed_rows:
        return
    project_root = globals().get("project_root") or Path(__file__).resolve().parent.parent
    global_path = project_root / "logs" / "dashboard_closed_pnl_history.jsonl"
    per_path_keys: dict[Path, set[str]] = {}
    for record in closed_rows:
        bot = record.get("bot_name") or record.get("account")
        symbol = record.get("symbol")
        tbid = record.get("trade_block_id")
        total_pnl = record.get("total_trade_pnl")
        status = record.get("status") or "closed"
        if not bot or not symbol or not tbid or total_pnl is None:
            continue
        bot_lower = str(bot).lower()
        symbol_upper = str(symbol).upper()
        key = f"{bot_lower}|{symbol_upper}|{tbid}"
        per_bot_path = project_root / "live_bots" / "100_50_hedge_bot" / bot_lower / "logs" / "dashboard_closed_pnl_history.jsonl"
        targets = [per_bot_path, global_path]
        written = False
        for target in targets:
            if target not in per_path_keys:
                per_path_keys[target] = _load_persisted_keys(target)
            if key in per_path_keys[target]:
                written = True
                break
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                payload: dict[str, Any] = {
                    "account": record.get("account") or bot,
                    "bot_name": bot_lower,
                    "symbol": symbol_upper,
                    "trade_block_id": tbid,
                    "status": status,
                    "total_trade_pnl": total_pnl,
                    "source": "dashboard_profit_trades_persist",
                    "created_at": record.get("created_at") or record.get("created_at_utc3") or "",
                    "updated_at": record.get("updated_at") or record.get("finalized_at") or "",
                }
                for field in (
                    "start_time",
                    "end_time",
                    "finalized_at",
                    "cycle_count",
                    "cycle_net_pnl",
                    "final_exit_net_pnl",
                    "final_long_exit_pnl",
                    "final_short_exit_pnl",
                    "pnl_source",
                    "breakdown",
                ):
                    if record.get(field) is not None:
                        payload[field] = record.get(field)
                _write_jsonl(target, payload)
                per_path_keys[target].add(key)
                logger.info(
                    "dashboard_profit_trade_persist_written %s %s",
                    str(target),
                    key,
                )
                written = True
                break
            except Exception as exc:
                logger.warning(
                    "dashboard_profit_trade_persist_failed %s %s %s",
                    str(target),
                    key,
                    exc,
                )
        if not written:
            logger.warning(
                "dashboard_profit_trade_persist_failed no_target_available %s",
                key,
            )



@app.get("/api/profit-verlauf/closed-pnl")
async def api_get_closed_pnl(
    user: dict = Depends(require_auth),
    account: str = Query("main", description="main, sub, Long_bot_1, Long_bot_2, Short_bot_1, Short_bot_2"),
    limit: int = Query(50, ge=1, le=100, description="Anzahl der Closed-PnL-Einträge (Default: 50)"),
):
    """
    Get Closed PnL für einen Bybit-Account.
    Unterstützt: main, sub, Long_bot_1, Long_bot_2, Short_bot_1, Short_bot_2.
    """
    logger.info(
        "[dashboard] dashboard_closed_pnl_request",
        {"account": acc if (acc := account.strip().lower() if account else account) else account, "limit": limit},
    )
    try:
        acc = account.strip()
        logger.info(
            "[dashboard] dashboard_closed_pnl_request",
            {"account": acc, "limit": limit},
        )
        if acc.lower() in ("main", "sub"):
            acc = acc.lower()
        if acc not in CLOSED_PNL_ACCOUNTS:
            return {"success": False, "error": f"account muss einer von {CLOSED_PNL_ACCOUNTS} sein"}
        api_key, secret_key = _get_account_keys(acc)
        if not api_key or not secret_key:
            return {"success": False, "error": f"API-Keys für {acc} fehlen"}
        import asyncio
        order_manager = BybitOrderManager(api_key, secret_key)
        records = await asyncio.to_thread(
            order_manager.get_closed_pnl,
            category="linear",
            limit=limit,
        )
        paths = get_account_pnl_paths(acc)
        def _closed_pnl_time(r):
            t = r.get("updatedTime") or r.get("createdTime") or 0
            try:
                return int(t)
            except (TypeError, ValueError):
                return 0
        records = sorted(records, key=_closed_pnl_time, reverse=True)
        margin_balance = None
        try:
            margin_balance = await asyncio.to_thread(order_manager.get_account_margin_balance)
        except Exception:
            pass
        if acc == "Long_bot_1":
            _maybe_run_dashboard_flat_snapshot(project_root)
        runtime_log_path = paths.get("runtime_log_path")
        dashboard_history_path = paths.get("dashboard_closed_pnl_history_path")
        if runtime_log_path and runtime_log_path.exists():
            _persist_closed_pnl_history_from_runtime_log(
                log_path=runtime_log_path,
                output_path=dashboard_history_path,
                account_label=acc,
            )
        order_purpose_map = _load_order_purpose_map_from_runtime_log(
            limit_lines=10000,
            log_path=runtime_log_path,
        )
        confirmed_rows = load_confirmed_order_pnl_rows(
            account=acc,
            path=paths.get("confirmed_pnl_history_path"),
        )
        logger.info(
            "[dashboard] dashboard_order_purpose_map_loaded",
            {
                "count": len(order_purpose_map),
                "sample_order_ids": list(order_purpose_map.keys())[:5],
                "sample_purposes": [
                    entry.get("purpose")
                    for entry in list(order_purpose_map.values())[:5]
                    if isinstance(entry, dict)
                ],
            },
        )
        closed_pnl_history = _load_dashboard_closed_pnl_history(
            account=acc,
            path=dashboard_history_path,
            limit=100,
        )
        fast_history_rows, duplicate_count = _merge_confirmed_order_pnl_rows_into_history(
            closed_pnl_history, confirmed_rows
        )
        if fast_history_rows:
            logger.info(
                "[dashboard] dashboard_closed_pnl_fast_rows_merged",
                {
                    "account": acc,
                    "added_fast_rows": len(fast_history_rows),
                    "source": "confirmed_order_pnl",
                },
            )
            closed_pnl_history = closed_pnl_history + fast_history_rows
            logger.info(
                "[dashboard] dashboard_confirmed_order_pnl_rows_used",
                {"account": acc, "fast_rows": len(fast_history_rows)},
            )
        if duplicate_count:
            logger.info(
                "[dashboard] dashboard_confirmed_order_pnl_duplicate_skipped",
                {"account": acc, "duplicate_count": duplicate_count},
            )
        strategy_state = _load_strategy_state_for_account(acc) or {}
        wallet_snapshot = _build_wallet_snapshot_payload(strategy_state, acc)
        snapshot_source = "live_state" if wallet_snapshot else None
        snapshot_file = None
        if not wallet_snapshot:
            logger.info(
                "[dashboard] wallet_snapshot_missing_before_ensure",
                {"account": acc, "snapshot_path": str(_wallet_snapshot_file_for_account(acc) or "")},
            )
            _ensure_wallet_snapshot_for_account(acc)
            snapshot_file = _wallet_snapshot_file_for_account(acc)
            file_snapshot = _load_wallet_snapshot_file(acc)
            if file_snapshot:
                wallet_snapshot = _build_wallet_snapshot_payload_from_file(file_snapshot, acc)
                snapshot_source = "snapshot_file"
            elif snapshot_file and snapshot_file.exists():
                try:
                    wallet_snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
                    snapshot_source = "snapshot_file"
                except Exception:
                    logger.warning(
                        "[dashboard] dashboard_wallet_snapshot_file_parse_failure",
                        {"account": acc, "file": str(snapshot_file)},
                    )
                snapshot_info = wallet_snapshot or {}
                logger.info(
                    "[dashboard] dashboard_wallet_snapshot_file_loaded",
                    {
                        "account": acc,
                        "file": str(snapshot_file) if snapshot_file else None,
                        "snapshot_phase": snapshot_info.get("snapshot_phase"),
                        "trade_block_id": snapshot_info.get("trade_block_id"),
                        "start_wallet_usdt": snapshot_info.get("wallet_balance_start_usdt"),
                        "current_wallet_usdt": snapshot_info.get("wallet_balance_current_usdt"),
                        "last_trade_profit_usdt": snapshot_info.get("last_trade_wallet_profit_usdt"),
                        "last_trade_profit_available": snapshot_info.get("last_trade_wallet_profit_available"),
                    },
                )
        snapshot_info = wallet_snapshot or {}
        snapshot_info = wallet_snapshot or {}
        if snapshot_source is None:
            snapshot_source = "fallback_current_wallet"
            logger.info(
                "[dashboard] dashboard_wallet_snapshot_file_missing",
                {"account": acc, "expected_file": str(snapshot_file) if snapshot_file else None},
            )
        logger.info(
            "[dashboard] dashboard_wallet_snapshot_source",
            {
                "account": acc,
                "source": snapshot_source,
                "wallet_snapshot_present": bool(wallet_snapshot),
                "last_trade_profit_available": snapshot_info.get("last_trade_wallet_profit_available"),
                "last_trade_wallet_profit_usdt": snapshot_info.get("last_trade_wallet_profit_usdt"),
                "wallet_balance_start_usdt": snapshot_info.get("wallet_balance_start_usdt"),
                "wallet_balance_current_usdt": snapshot_info.get("wallet_balance_current_usdt"),
            },
        )
        cycle_entries = _extract_cycle_pnl_entries(strategy_state)
        final_exit_pnl = _extract_final_exit_pnl(strategy_state)
        return {
            "success": True,
            "list": records,
            "confirmed_pnl_rows": confirmed_rows,
            "order_purpose_map": order_purpose_map,
            "margin_balance": round(margin_balance, 2) if margin_balance is not None else None,
            "wallet_snapshot": wallet_snapshot,
            "cycle_pnl_entries": cycle_entries,
            "final_exit_pnl": final_exit_pnl,
            "closed_pnl_history": closed_pnl_history,
        }
    except Exception as e:
        logger.error(f"Fehler beim Abrufen der Closed PnL: {e}", exc_info=True)
        return {"success": False, "error": str(e), "list": []}


@app.get("/api/profit-summary")
async def api_profit_summary(
    profile: str = Query("bot_1", description="Bot profile (e.g. bot_1)"),
    limit: int = Query(100, ge=1, le=500, description="Number of trades to return"),
    user: dict = Depends(require_auth),
):
    """Return aggregated trade-block summaries grouped by trade_block_id."""
    trades, warnings = _load_trade_blocks_for_profile(profile, limit)
    summary = _summarize_trade_blocks(trades)
    return {
        "profile": profile,
        "summary": summary,
        "trades": trades,
        "warnings": warnings,
    }


@app.post("/api/hedge/close-positions/{symbol}")
async def api_close_hedge_positions(
    symbol: str,
    user: dict = Depends(require_auth)
):
    """Close both Long and Short positions for a symbol - via Master Bot API"""
    try:
        logger.info("=" * 80)
        logger.info(f"🔴 Schließe Positionen für {symbol} (via Master Bot API)")
        logger.info("=" * 80)
        
        # Generiere Request-ID für Idempotenz
        request_id = str(uuid.uuid4())
        logger.info(f"📋 Request-ID: {request_id}")
        
        # Rufe Master Bot API auf
        logger.info(f"🌐 Rufe Master Bot API auf: {MASTER_BOT_API_URL}/master/close-positions")
        
        async with httpx.AsyncClient(timeout=300.0) as client:  # 5 Minuten Timeout für VPN/Bybit API
            try:
                response = await client.post(
                    f"{MASTER_BOT_API_URL}/master/close-positions",
                    json={"symbol": symbol},
                    headers={
                        "X-Request-ID": request_id,
                        "X-Internal-Token": MASTER_BOT_API_TOKEN,
                        "Content-Type": "application/json"
                    }
                )
                
                logger.info(f"📥 API Response Status: {response.status_code}")
                
                if response.status_code == 200:
                    api_response = response.json()
                    logger.info(f"📤 API Response erhalten")
                    
                    if api_response.get("success"):
                        # API-Response hat bereits das richtige Format
                        data = api_response.get("data", {})
                        return {
                            "success": True,
                            "main_account": data.get("main_account", {}),
                            "sub_account": data.get("sub_account", {}),
                            "message": api_response.get("message", "Positionen geschlossen")
                        }
                    else:
                        # API-Response hat Fehler
                        error_code = api_response.get("error_code", "UNKNOWN")
                        error_message = api_response.get("message", "Unknown error")
                        logger.error(f"❌ Master Bot API Error ({error_code}): {error_message}")
                        
                        # Gebe Fehler als JSON-Response zurück (statt HTTPException)
                        return {
                            "success": False,
                            "error": error_message,
                            "error_code": error_code
                        }
                else:
                    # HTTP-Status != 200
                    try:
                        error_response = response.json()
                        error_message = error_response.get("message", f"HTTP {response.status_code}")
                        logger.error(f"❌ HTTP Error {response.status_code}: {error_message}")
                        return {
                            "success": False,
                            "error": error_message
                        }
                    except:
                        logger.error(f"❌ HTTP Error {response.status_code}: {response.text}")
                        return {
                            "success": False,
                            "error": f"Master Bot API error: {response.text}"
                        }
                        
            except HTTPException as e:
                # Konvertiere HTTPException zu JSON-Response für Frontend
                logger.error(f"❌ HTTPException beim Schließen der Positionen: {e.detail}")
                return {
                    "success": False,
                    "error": e.detail
                }
            except httpx.HTTPError as e:
                logger.error(f"❌ HTTP-Error beim Aufruf der Master Bot API: {e}", exc_info=True)
                return {
                    "success": False,
                    "error": f"Master Bot API nicht erreichbar: {str(e)}"
                }
            except httpx.TimeoutException:
                logger.error(f"❌ Timeout beim Aufruf der Master Bot API")
                return {
                    "success": False,
                    "error": "Master Bot API Timeout - Bitte versuchen Sie es erneut"
                }
                
    except HTTPException as e:
        # Konvertiere HTTPException zu JSON-Response für Frontend
        logger.error(f"❌ HTTPException beim Schließen der Positionen: {e.detail}")
        return {
            "success": False,
            "error": e.detail
        }


@app.post("/api/hedge/close-positions-main/{symbol}")
async def api_close_positions_main(
    symbol: str,
    user: dict = Depends(require_auth),
    profile: Optional[str] = Query(None, description="main|bot_1|bot_2 – bestimmt Account-Kombo aus config.yaml/profiles"),
):
    """Schließt alle Positionen (Long + Short) auf dem Main-Account bzw. Profil-Long-Account (bot_1/bot_2)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    symbol = symbol.strip().upper()
    if not symbol:
        return {"success": False, "error": "Symbol fehlt"}

    prof = (profile or "").strip().lower()
    if prof in ("bot_1", "bot_2"):
        # Verwende Long-Account des gewählten Profils (z.B. Long_bot_1)
        main_key, main_sec, sub_key, sub_sec = _get_account_keys_by_profile(prof)
        main_api_key, main_secret_key = main_key, main_sec
    else:
        main_api_key, main_secret_key = _get_account_keys("main")
    if not main_api_key or not main_secret_key:
        return {"success": False, "error": "Main-Account API-Keys fehlen"}
    try:
        main_om = BybitOrderManager(main_api_key, main_secret_key)
        result = await asyncio.to_thread(main_om.close_all_positions, symbol)
        return {
            "success": result.get("success", False),
            "long_closed": result.get("long_closed", False),
            "short_closed": result.get("short_closed", False),
            "errors": result.get("errors", []),
            "message": "Main-Account Positionen geschlossen" if result.get("success") else ("; ".join(result.get("errors", [])) or "Keine Positionen geschlossen")
        }
    except Exception as e:
        logger.error(f"Fehler beim Schließen der Main-Positionen für {symbol}: {e}", exc_info=True)
        return {"success": False, "error": str(e), "errors": [str(e)]}


@app.post("/api/hedge/close-positions-sub-test/{symbol}")
async def api_close_positions_sub_test(
    symbol: str,
    user: dict = Depends(require_auth),
    profile: Optional[str] = Query(None, description="main|bot_1|bot_2 – bestimmt Account-Kombo aus config.yaml/profiles"),
):
    """Test: Schließt alle Positionen (Long + Short) auf dem Sub-Account bzw. Profil-Short-Account (bot_1/bot_2)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    symbol = symbol.strip().upper()
    if not symbol:
        return {"success": False, "error": "Symbol fehlt"}

    prof = (profile or "").strip().lower()
    if prof in ("bot_1", "bot_2"):
        main_key, main_sec, sub_key, sub_sec = _get_account_keys_by_profile(prof)
        sub_api_key, sub_secret_key = sub_key, sub_sec
    else:
        sub_api_key, sub_secret_key = _get_account_keys("sub")
    if not sub_api_key or not sub_secret_key:
        return {"success": False, "error": "Sub-Account API-Keys fehlen"}
    try:
        sub_om = BybitOrderManager(sub_api_key, sub_secret_key)
        result = await asyncio.to_thread(sub_om.close_all_positions, symbol)
        return {
            "success": result.get("success", False),
            "long_closed": result.get("long_closed", False),
            "short_closed": result.get("short_closed", False),
            "errors": result.get("errors", []),
            "message": "Sub-Account Positionen geschlossen (Test)" if result.get("success") else ("; ".join(result.get("errors", [])) or "Keine Positionen geschlossen")
        }
    except Exception as e:
        logger.error(f"Fehler beim Schließen der Sub-Positionen (Test) für {symbol}: {e}", exc_info=True)
        return {"success": False, "error": str(e), "errors": [str(e)]}


@app.post("/api/hedge/start-dual-bots")
async def api_start_dual_bots(
    payload: dict = Body(...),
    user: dict = Depends(require_auth)
):
    """Start dual bots via start_main_long__sub_short.sh or start_both_bots (bei Profil bot_1/bot_2)."""
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        return {"success": False, "error": "Symbol fehlt"}
    profile = (payload.get("profile") or "").strip() or None
    if profile not in ("main", "bot_1", "bot_2"):
        profile = None
    # (C) Block if per-symbol configs are missing for either bot.
    long_cfg = get_config_path(bot_type="long", symbol=symbol, profile=profile)
    short_cfg = get_config_path(bot_type="short", symbol=symbol, profile=profile)
    missing = [str(p) for p in (long_cfg, short_cfg) if not p.exists()]
    if missing:
        return {
            "success": False,
            "error": f"Config fehlt für {symbol}: {', '.join(missing)}. Bitte erst im Dashboard speichern/anlegen.",
            "error_code": "MISSING_SYMBOL_CONFIG",
            "missing": missing,
        }
    try:
        if profile and profile in ("bot_1", "bot_2"):
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None,
                _start_both_bots_via_script_blocking,
                symbol,
                None, None, profile
            )
            all_ok = results["long"]["success"] and results["short"]["success"]
            return {"success": all_ok, "symbol": symbol, "results": results}
        script_path = project_root / "start_main_long__sub_short.sh"
        if not script_path.exists():
            return {"success": False, "error": f"Script nicht gefunden: {script_path}"}
        cmd = [str(script_path), "--daemon", symbol]
        subprocess.Popen(
            cmd,
            cwd=str(project_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return {"success": True, "symbol": symbol}
    except Exception as e:
        logger.error(f"Fehler beim Starten der Bots via Script: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.post("/api/hedge/start-bot-script")
async def api_start_bot_script(
    payload: dict = Body(...),
    user: dict = Depends(require_auth)
):
    """Start single bot via script with symbol + size override."""
    bot_type = str(payload.get("bot_type") or "").strip().lower()
    symbol = str(payload.get("symbol") or "").strip().upper()
    size_raw = str(payload.get("size") or "").strip()
    profile = _normalize_dashboard_profile((payload.get("profile") or "").strip() or None, fallback_to_main=False)
    profile_record = resolve_profile_to_bot_record(profile) if _is_registry_bot_profile(profile) else None
    if bot_type not in {"long", "short"}:
        return {"success": False, "error": "bot_type muss 'long' oder 'short' sein"}
    if not symbol:
        return {"success": False, "error": "Symbol fehlt"}

    if profile_record and bot_type == "long":
        project_root = Path(__file__).resolve().parent.parent
        _maybe_run_dashboard_start_snapshot(profile_record["bot_name"], project_root=project_root)
        script_path = _long_bot_shared_script_path("start")
        if not _is_executable_script(script_path):
            return {"success": False, "error": f"Script nicht gefunden: {script_path}"}
        return _start_long_bot_script_async(script_path, profile_record["bot_name"], "start_long_bot", project_root)

    # (C) Block start if per-symbol config is missing (prevents using wrong global config).
    cfg_path = get_config_path(bot_type=bot_type, symbol=symbol, profile=profile)
    if not cfg_path.exists():
        return {
            "success": False,
            "error": f"Config fehlt für {bot_type}@{symbol}: {cfg_path}. Bitte erst im Dashboard speichern/anlegen.",
            "error_code": "MISSING_SYMBOL_CONFIG",
            "config_path": str(cfg_path),
        }
    start_key = f"{bot_type}:{symbol}:{profile or 'main'}"

    with _BOT_START_LOCK:
        if start_key in _BOT_START_IN_PROGRESS:
            return {
                "success": True,
                "already_running": True,
                "message": f"{bot_type.capitalize()} Bot {symbol}: Start läuft bereits"
            }
        _BOT_START_IN_PROGRESS.add(start_key)

    try:
        # Guard: niemals doppelte Bot-Prozesse starten
        if is_bot_running(symbol, bot_type, profile=profile):
            return {
                "success": True,
                "already_running": True,
                "message": f"{bot_type.capitalize()} Bot {symbol} läuft bereits"
            }

        # Strikte Config-Only-Policy: Size NUR aus Config, nie aus Payload
        cfg_profile = profile if _is_registry_bot_profile(profile) else None
        bot_cfg = load_config(symbol=symbol, bot_type=bot_type, profile=cfg_profile) or {}
        try:
            size_val = float(bot_cfg.get("initial_long_usdt" if bot_type == "long" else "initial_short_usdt", 20))
            if size_val <= 0:
                size_val = 20.0
        except (TypeError, ValueError):
            size_val = 20.0

        # Double-check direkt vor Start (gegen Race mit externen Starts)
        if is_bot_running(symbol, bot_type, profile=profile):
            return {
                "success": True,
                "already_running": True,
                "message": f"{bot_type.capitalize()} Bot {symbol} läuft bereits"
            }

        # start_config.yaml immer passend zum Profil aktualisieren (Main: config/start_config.yaml, Profile: config/<profile>/start_config.yaml)
        start_config_dir = project_root / "config"
        cfg_profile = profile if _is_registry_bot_profile(profile) else None
        if cfg_profile:
            start_config_dir = start_config_dir / cfg_profile
        start_config_dir.mkdir(parents=True, exist_ok=True)
        start_config_path = start_config_dir / "start_config.yaml"
        long_cfg = load_config(symbol=symbol, bot_type="long", profile=cfg_profile) or {}
        short_cfg = load_config(symbol=symbol, bot_type="short", profile=cfg_profile) or {}
        start_data = {
            "symbol": symbol,
            "long_bot": {"initial_usdt": round(float(long_cfg.get("initial_long_usdt", 20)), 2)},
            "short_bot": {"initial_usdt": round(float(short_cfg.get("initial_short_usdt", 20)), 2)},
        }
        if bot_type == "long":
            start_data["long_bot"]["initial_usdt"] = round(size_val, 2)
        else:
            start_data["short_bot"]["initial_usdt"] = round(size_val, 2)
        with open(start_config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(start_data, f, default_flow_style=False, allow_unicode=True)

        if profile and _is_registry_bot_profile(profile):
            script_name = f"start_{bot_type}_bot_{profile[-1]}.sh"
        else:
            script_name = "start_long_main.sh" if bot_type == "long" else "start_short_sub.sh"
        script_path = project_root / script_name
        if not script_path.exists():
            return {"success": False, "error": f"Script nicht gefunden: {script_path}"}

        # Size nur für diesen Lauf an Script übergeben – initial_short_usdt/initial_long_usdt in Config
        # nicht überschreiben, damit die „Start-Größe“ (z. B. 400) für Auto-Restart erhalten bleibt.
        cmd = [str(script_path), "--restart", "--daemon", symbol, str(size_val)]
        run_env = {**os.environ, "PYTHONPATH": str(project_root)}
        if profile and _is_registry_bot_profile(profile):
            run_env["HEDGE_PROFILE"] = profile
        subprocess.Popen(
            cmd,
            cwd=str(project_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=run_env
        )
        # Hedge Guardian mitstarten (main für Long, sub für Short)
        _start_hedge_guardian_after_bots_async("main" if bot_type == "long" else "sub", symbol=symbol)
        return {"success": True, "symbol": symbol, "bot_type": bot_type, "size": size_val}
    except Exception as e:
        logger.error(f"Fehler beim Starten des Bots via Script: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
    finally:
        with _BOT_START_LOCK:
            _BOT_START_IN_PROGRESS.discard(start_key)


@app.post("/api/hedge/start-bot-at-price")
async def api_start_bot_at_price(
    payload: dict = Body(...),
    user: dict = Depends(require_auth)
):
    """Startet den Long- oder Short-Bot, sobald der Marktpreis den Zielpreis erreicht (Script im Hintergrund)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    bot_type = str((payload.get("bot_type") or "").strip().lower())
    symbol = str((payload.get("symbol") or "").strip().upper())
    try:
        target_price = float(payload.get("target_price"))
    except (TypeError, ValueError):
        return {"success": False, "error": "target_price fehlt oder ungültig"}
    if bot_type not in ("long", "short"):
        return {"success": False, "error": "bot_type muss 'long' oder 'short' sein"}
    if not symbol:
        return {"success": False, "error": "symbol fehlt"}
    if target_price <= 0:
        return {"success": False, "error": "target_price muss > 0 sein"}
    trigger = str((payload.get("trigger") or "").strip().lower() or ("below" if bot_type == "long" else "above"))
    if trigger not in ("above", "below"):
        trigger = "below" if bot_type == "long" else "above"
    profile = _normalize_dashboard_profile((payload.get("profile") or "").strip() or None, fallback_to_main=False)
    _project_root = Path(__file__).resolve().parent.parent
    script_path = _project_root / "scripts" / "start_bot_at_price.py"
    if not script_path.exists():
        return {"success": False, "error": f"Script nicht gefunden: {script_path}"}
    _state_file = _project_root / "data" / "state" / "start_bot_at_price.json"
    run_env = {**os.environ, "PYTHONPATH": str(_project_root)}
    if profile and _is_registry_bot_profile(profile):
        run_env["HEDGE_PROFILE"] = profile
    cmd = [sys.executable, str(script_path), "--bot", bot_type, "--symbol", symbol, "--target-price", str(target_price), "--trigger", trigger]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_project_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=run_env,
        )
        try:
            _state_file.parent.mkdir(parents=True, exist_ok=True)
            prof_key = profile if _is_registry_bot_profile(profile) else "main"
            existing = _load_price_at_state_all_profiles()
            if prof_key not in existing:
                existing[prof_key] = {"long": {}, "short": {}}
            existing[prof_key].setdefault("long", {})
            existing[prof_key].setdefault("short", {})
            existing[prof_key][bot_type][symbol] = {
                "pid": proc.pid,
                "target_price": target_price,
                "trigger": trigger,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            }
            with open(_state_file, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            logger.warning(f"State-Datei beim Start nicht geschrieben: {e}")
        logger.info(f"Start-Bot-at-Price gestartet: {bot_type} {symbol} @ {target_price} ({trigger})")
        return {"success": True, "message": f"{bot_type.capitalize()}-Bot startet, wenn Preis {trigger} {target_price}.", "symbol": symbol, "bot_type": bot_type}
    except Exception as e:
        logger.error(f"Fehler beim Starten von start_bot_at_price: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


_PRICE_AT_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "state" / "start_bot_at_price.json"


def _load_price_at_state_all_profiles() -> dict:
    """Lädt komplette State-Datei. Altes Format {long, short} → als 'main' behandelt."""
    if not _PRICE_AT_STATE_FILE.exists():
        return {}
    try:
        with open(_PRICE_AT_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "main" in data or "bot_1" in data or "bot_2" in data:
            return data
        return {
            "main": {
                "long": data.get("long") or {},
                "short": data.get("short") or {},
            }
        }
    except Exception:
        return {}


def _load_price_at_state_raw(profile: Optional[str] = None):
    """Lädt Price-at für ein Profil. profile=main|bot_1|bot_2. Ohne Bereinigung."""
    all_data = _load_price_at_state_all_profiles()
    prof = (profile or "").strip() if profile else None
    if prof not in ("main", "bot_1", "bot_2"):
        prof = "main"
    section = all_data.get(prof) or {}
    return {
        "long": section.get("long") or {},
        "short": section.get("short") or {},
    }


def _load_price_at_state():
    """Lädt State; bereinigt tote PIDs. Unterstützt Profil-Struktur."""
    data = _load_price_at_state_all_profiles()
    if not data:
        return {"long": {}, "short": {}}
    changed = False
    for prof_key in list(data.keys()):
        section = data[prof_key] or {}
        for bot_type in ("long", "short"):
            for symbol in list((section.get(bot_type) or {}).keys()):
                entry = section[bot_type][symbol]
                pid = entry.get("pid")
                if pid is None:
                    section[bot_type].pop(symbol, None)
                    changed = True
                    continue
                try:
                    os.kill(int(pid), 0)
                except (OSError, ProcessLookupError, ValueError):
                    section[bot_type].pop(symbol, None)
                    changed = True
    if changed:
        try:
            _PRICE_AT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_PRICE_AT_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
    prof_data = data.get("main") or data
    if isinstance(prof_data, dict) and "long" in prof_data:
        return prof_data
    return {"long": {}, "short": {}}


@app.get("/api/hedge/start-bot-at-price-status")
async def api_start_bot_at_price_status(user: dict = Depends(require_auth)):
    """Liefert aktive Price-at-Läufe (long/short pro Symbol) für Dashboard-Anzeige."""
    data = _load_price_at_state()
    return {"success": True, "price_at": data}


@app.post("/api/hedge/stop-bot-at-price")
async def api_stop_bot_at_price(
    payload: dict = Body(...),
    user: dict = Depends(require_auth)
):
    """Beendet den Price-at-Warteprozess für den angegebenen Bot und Symbol."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    bot_type = str((payload.get("bot_type") or "").strip().lower())
    symbol = str((payload.get("symbol") or "").strip().upper())
    profile = (payload.get("profile") or "").strip() or None
    if profile not in ("main", "bot_1", "bot_2"):
        profile = "main"
    if bot_type not in ("long", "short"):
        return {"success": False, "error": "bot_type muss 'long' oder 'short' sein"}
    if not symbol:
        return {"success": False, "error": "symbol fehlt"}
    data = _load_price_at_state_all_profiles()
    entry = None
    found_prof = None
    for try_prof in [profile, "main"]:
        section = data.get(try_prof) or {}
        bt_data = section.get(bot_type) or {}
        if symbol in bt_data:
            entry = bt_data[symbol]
            found_prof = try_prof
            break
    if not entry:
        return {"success": True, "message": "Kein Price-at-Lauf für diesen Bot/Symbol aktiv."}
    pid = entry.get("pid")
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError, ValueError) as e:
        logger.warning(f"Stop Price-at: Prozess {pid} nicht erreichbar: {e}")
    try:
        if found_prof and found_prof in data:
            data[found_prof][bot_type].pop(symbol, None)
        _PRICE_AT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_PRICE_AT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"State-Datei nach Stop nicht geschrieben: {e}")
    logger.info(f"Price-at gestoppt: {bot_type} {symbol} (pid={pid})")
    return {"success": True, "message": f"Price-at für {bot_type.capitalize()} Bot ({symbol}) beendet."}


@app.post("/api/hedge/restart-long-auto")
async def api_restart_long_auto(
    payload: dict = Body(default={}),
    user: dict = Depends(require_auth)
):
    """
    Restart Long Bot with optional symbol/size.
    If symbol is empty, auto-detect from active main-account positions.
    Optional: long_tp_percentage, short_tp_percentage, burns_before_rebuy, burn_mode
    werden vor dem Restart in die Config geschrieben (erspart separaten set-tp-config-Aufruf).
    """
    input_symbol = str((payload or {}).get("symbol") or "").strip().upper()
    symbol = input_symbol

    if not symbol:
        try:
            main_api_key, main_secret_key = _get_account_keys("main")
            if not main_api_key or not main_secret_key:
                return {"success": False, "error": "Main-API-Keys fehlen für Symbol-Auto-Detect"}
            main_order_manager = BybitOrderManager(main_api_key, main_secret_key)
            positions = await asyncio.to_thread(main_order_manager.fetch_positions_direct, None, 5)
            active_symbols = set()
            for pos in positions or []:
                info = pos.get("info", {})
                pos_symbol = str(info.get("symbol") or "").strip().upper()
                try:
                    size = float(info.get("size") or 0.0)
                except Exception:
                    size = 0.0
                if pos_symbol and size > 0:
                    active_symbols.add(pos_symbol)

            if len(active_symbols) == 1:
                symbol = list(active_symbols)[0]
            elif len(active_symbols) == 0:
                return {"success": False, "error": "Kein aktives Symbol gefunden. Bitte Symbol eingeben."}
            else:
                return {"success": False, "error": f"Mehrere aktive Symbole gefunden: {sorted(active_symbols)}"}
        except Exception as e:
            logger.error(f"Fehler beim Symbol-Auto-Detect (Long Restart): {e}", exc_info=True)
            return {"success": False, "error": "Symbol-Auto-Detect fehlgeschlagen"}

    # Kein Config-Update vor Restart – Bot nutzt strikt nur Config, keine Form/Payload-Daten

    profile = (payload or {}).get("profile")
    profile = profile if profile in ("bot_1", "bot_2") else None

    # Restart-Semantik: zuerst stoppen (Fast-Path für skriptgestartete Bots, sonst systemctl + Fallback)
    try:
        if profile:
            stopped = await asyncio.to_thread(_stop_script_bot, symbol, "long", profile)
        else:
            stopped = await _stop_bot_for_restart(symbol, "long")
        if not stopped:
            return {
                "success": False,
                "error": "Long Bot konnte nicht gestoppt werden (systemctl und PID-Fallback versucht). Prozess manuell prüfen oder neu starten."
            }
    except Exception as e:
        logger.error(f"Fehler beim Stop vor Long-Restart ({symbol}): {e}", exc_info=True)
        return {"success": False, "error": f"Stop vor Restart fehlgeschlagen: {e}"}

    # Start mit vorhandener Start-Logik – Size strikt aus Config
    result = await api_start_bot_script(
        payload={"bot_type": "long", "symbol": symbol, "profile": profile},
        user=user
    )
    if isinstance(result, dict):
        result.setdefault("symbol", symbol)
    # Nach erfolgreichem Long-Restart nur Main-Guardian starten (Long läuft auf Main) – mit Symbol
    if isinstance(result, dict) and result.get("success"):
        _start_hedge_guardian_after_bots_async("main", symbol=symbol)
    return result


@app.post("/api/hedge/restart-short-auto")
async def api_restart_short_auto(
    payload: dict = Body(default={}),
    user: dict = Depends(require_auth)
):
    """
    Restart Short Bot with optional symbol/size.
    If symbol is empty, auto-detect from active sub-account positions.
    """
    input_symbol = str((payload or {}).get("symbol") or "").strip().upper()
    symbol = input_symbol

    if not symbol:
        try:
            sub_api_key, sub_secret_key = _get_account_keys("sub")
            if not sub_api_key or not sub_secret_key:
                return {"success": False, "error": "Sub-API-Keys fehlen für Symbol-Auto-Detect"}
            sub_order_manager = BybitOrderManager(sub_api_key, sub_secret_key)
            positions = await asyncio.to_thread(sub_order_manager.fetch_positions_direct, None, 5)
            active_symbols = set()
            for pos in positions or []:
                info = pos.get("info", {})
                pos_symbol = str(info.get("symbol") or "").strip().upper()
                try:
                    size = float(info.get("size") or 0.0)
                except Exception:
                    size = 0.0
                if pos_symbol and size > 0:
                    active_symbols.add(pos_symbol)

            if len(active_symbols) == 1:
                symbol = list(active_symbols)[0]
            elif len(active_symbols) == 0:
                return {"success": False, "error": "Kein aktives Symbol gefunden. Bitte Symbol eingeben."}
            else:
                return {"success": False, "error": f"Mehrere aktive Symbole gefunden: {sorted(active_symbols)}"}
        except Exception as e:
            logger.error(f"Fehler beim Symbol-Auto-Detect (Short Restart): {e}", exc_info=True)
            return {"success": False, "error": "Symbol-Auto-Detect fehlgeschlagen"}

    # Kein Config-Update vor Restart – Bot nutzt strikt nur Config, keine Form/Payload-Daten

    profile = (payload or {}).get("profile")
    profile = profile if profile in ("bot_1", "bot_2") else None

    # Restart-Semantik: zuerst stoppen (Fast-Path für skriptgestartete Bots, sonst systemctl + Fallback)
    try:
        if profile:
            stopped = await asyncio.to_thread(_stop_script_bot, symbol, "short", profile)
        else:
            stopped = await _stop_bot_for_restart(symbol, "short")
        if not stopped:
            return {
                "success": False,
                "error": "Short Bot konnte nicht gestoppt werden (systemctl und PID-Fallback versucht). Prozess manuell prüfen oder neu starten."
            }
    except Exception as e:
        logger.error(f"Fehler beim Stop vor Short-Restart ({symbol}): {e}", exc_info=True)
        return {"success": False, "error": f"Stop vor Restart fehlgeschlagen: {e}"}

    # Start mit vorhandener Start-Logik – Size strikt aus Config
    result = await api_start_bot_script(
        payload={"bot_type": "short", "symbol": symbol, "profile": profile},
        user=user
    )
    if isinstance(result, dict):
        result.setdefault("symbol", symbol)
    # Nach erfolgreichem Short-Restart nur Sub-Guardian starten (Short läuft auf Sub)
    if isinstance(result, dict) and result.get("success"):
        _start_hedge_guardian_after_bots_async("sub")
    return result


# API Endpoints
@app.get("/api/bots")
async def api_bots(user: dict = Depends(require_auth), bot_type: str = Query(None)):
    """Get all bots"""
    bots = get_all_bots(bot_type=bot_type)
    for bot in bots:
        bot_type_for_bot = bot.get("bot_type", "long")
        bot["state"] = load_bot_state(bot["symbol"], bot_type=bot_type_for_bot)
    return {"bots": bots}


@app.get("/api/bots/{symbol}")
async def api_bot_status(symbol: str, user: dict = Depends(require_auth), bot_type: str = Query("long")):
    """Get bot status with full data"""
    bot = get_bot_status(symbol, bot_type=bot_type)
    bot["state"] = load_bot_state(symbol, bot_type=bot_type)
    bot["config"] = load_config(symbol=symbol, bot_type=bot_type, fallback_to_global=True)
    
    # Get position info (uses log parsing for speed)
    current_time = time.time()
    position_info = None
    
    # Check if log file has changed
    log_key = f"{symbol}_{bot_type}"
    last_mtime = log_file_mtimes.get(log_key)
    log_changed, current_mtime = has_log_file_changed(symbol, last_mtime, bot_type=bot_type)
    log_file_mtimes[log_key] = current_mtime
    
    # Check cache
    cache_key = f"{symbol}_{bot_type}"
    if cache_key in position_cache:
        cached_data, cache_time = position_cache[cache_key]
        cache_age = current_time - cache_time
        if cache_age < position_cache_timeout and not log_changed:
            position_info = cached_data
        elif cache_age < 5:
            position_info = cached_data
        else:
            position_info = None
    else:
        position_info = None
    
    # Fetch new data if needed
    if not position_info:
        try:
            position_info = get_position_info(symbol, bot_type=bot_type)
            position_cache[cache_key] = (position_info, current_time)
        except Exception as e:
            # Use cached data if available
            if cache_key in position_cache:
                position_info = position_cache[cache_key][0]
            else:
                position_info = None
    
    bot["position"] = position_info
    
    # Calculate next burn
    if position_info:
        try:
            bot["next_burn"] = calculate_next_burn_size_from_position(symbol, position_info)
        except Exception:
            bot["next_burn"] = {"valid": False}
    else:
        bot["next_burn"] = {"valid": False}
    
    # Calculate rebuy info if rebuy is upcoming (next_rebuy_in == 0)
    try:
        if bot.get("state", {}).get("next_rebuy_in") == 0:
            if position_info:
                bot["rebuy_info"] = calculate_rebuy_info(symbol, position_info, bot.get("state"))
            else:
                bot["rebuy_info"] = {"valid": False}
        else:
            bot["rebuy_info"] = {"valid": False}
    except Exception:
        bot["rebuy_info"] = {"valid": False}
    
    return bot


@app.get("/api/bots/{symbol}/cycle")
async def api_bot_cycle(symbol: str, user: dict = Depends(require_auth), bot_type: str = Query("long")):
    """Get bot cycle status"""
    state = load_bot_state(symbol, bot_type=bot_type)
    return state


@app.get("/api/positions/{symbol}")
async def api_get_positions(symbol: str, user: dict = Depends(require_auth), bot_type: str = Query("long")):
    """Get current position data for a symbol - used by Position Calculator"""
    try:
        position_info = get_position_info(symbol, bot_type=bot_type)
        
        # Extract the data we need for the calculator
        long_data = position_info.get("long", {})
        short_data = position_info.get("short", {})
        
        return {
            "success": True,
            "long": {
                "size": long_data.get("size"),
                "entry_price": long_data.get("entry_price")
            },
            "short": {
                "size": short_data.get("size"),
                "entry_price": short_data.get("entry_price")
            }
        }
    except Exception as e:
        import logging
        logging.error(f"Error getting positions for {symbol}: {e}")
        return {
            "success": False,
            "error": str(e),
            "long": {"size": None, "entry_price": None},
            "short": {"size": None, "entry_price": None}
        }


@app.post("/api/bots/{symbol}/start")
async def api_bot_start(symbol: str, request: BotActionRequest, user: dict = Depends(require_auth)):
    """Start bot and ensure watcher is running"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        bot_type = request.bot_type
        profile = _normalize_dashboard_profile(request.profile, fallback_to_main=False)
        profile_record = resolve_profile_to_bot_record(profile) if _is_registry_bot_profile(profile) else None

        if profile_record and bot_type == "long":
            project_root = Path(__file__).resolve().parent.parent
            _maybe_run_dashboard_start_snapshot(profile_record["bot_name"], project_root=project_root)
            script_path = _long_bot_shared_script_path("start")
            if not _is_executable_script(script_path):
                return {"success": False, "message": f"Script nicht gefunden: {script_path}"}
            return _start_long_bot_script_async(script_path, profile_record["bot_name"], "start_long_bot", project_root)

        service_name = f'hedgebot-{bot_type}@{symbol}'

        # (C) Block start if per-symbol config is missing to avoid using wrong global config.
        cfg_path = get_config_path(bot_type=bot_type, symbol=symbol)
        if not cfg_path.exists():
            return {
                "success": False,
                "message": f"Config fehlt für {bot_type}@{symbol}: {cfg_path}. Bitte erst im Dashboard speichern/anlegen.",
                "error_code": "MISSING_SYMBOL_CONFIG",
                "config_path": str(cfg_path),
            }
        
        # Check if bot is already running
        from utils.bot_monitor import is_bot_running
        if is_bot_running(symbol, bot_type):
            return {"success": True, "message": f"{bot_type.capitalize()} Bot {symbol} läuft bereits", "already_running": True}
        
        # Starte Bot
        result = run_sudo_command(
            ['sudo', 'systemctl', 'start', service_name],
            timeout=10
        )
        
        if result.returncode == 0:
            # Wait a bit for service to start
            import time
            time.sleep(1)
            
            # Check service status for better error reporting
            status_result = run_sudo_command(
                ['sudo', 'systemctl', 'status', service_name, '--no-pager'],
                timeout=5
            )
            
            # Check if service is actually active
            is_active = is_bot_running(symbol, bot_type)
            
            if is_active:
                # Hedge Guardian mitstarten (main für Long, sub für Short) – mit Symbol für diesen Coin
                _start_hedge_guardian_after_bots_async("main" if bot_type == "long" else "sub", symbol=symbol)
                # NOTE: Watcher startet sich selbst autonom - keine manuelle Steuerung mehr nötig
                # Send alert
                send_bot_alert(symbol, "started", f"{bot_type.capitalize()} Bot {symbol} wurde gestartet")
                return {"success": True, "message": f"{bot_type.capitalize()} Bot {symbol} started"}
            else:
                # Service started but not active - might be crashing
                error_msg = f"Service gestartet, aber Bot läuft nicht. Status: {status_result.stdout[:200] if status_result.stdout else 'Unbekannt'}"
                logger.error(f"Bot {symbol} ({bot_type}) start failed: {error_msg}")
                return {"success": False, "message": error_msg}
        else:
            error_msg = result.stderr or result.stdout or "Unbekannter Fehler"
            logger.error(f"Failed to start bot {symbol} ({bot_type}): {error_msg}")
            return {"success": False, "message": f"systemctl start fehlgeschlagen: {error_msg}"}
    except Exception as e:
        logger.error(f"Exception starting bot {symbol} ({bot_type}): {e}", exc_info=True)
        return {"success": False, "message": str(e)}


@app.post("/api/bots/{symbol}/stop")
async def api_bot_stop(symbol: str, request: BotActionRequest, user: dict = Depends(require_auth)):
    """Stop bot (blockierende systemctl-Aufrufe laufen im Thread, damit die API reagibel bleibt).
    Bei profile=bot_1/bot_2: Script-basierter Stop via short_bot_SYMBOL_bot_1.pid."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    prof = _normalize_dashboard_profile(request.profile, fallback_to_main=False)
    profile_record = resolve_profile_to_bot_record(prof) if _is_registry_bot_profile(prof) else None
    
    try:
        bot_type = request.bot_type
        service_name = f'hedgebot-{bot_type}@{symbol}'
        
        # bot_1/bot_2: Script-gestartete Bots haben kein systemctl → direkt _stop_script_bot
        if profile_record and bot_type == "long":
            stop_result = await asyncio.to_thread(_stop_script_bot, symbol.strip().upper(), bot_type, prof)
            await asyncio.sleep(0.2)
            if stop_result and not await asyncio.to_thread(is_bot_running, symbol, bot_type, prof):
                send_bot_alert(symbol, "stopped", f"{bot_type.capitalize()} Bot {symbol} ({prof}) wurde gestoppt")
                _stop_hedge_guardian_if_no_bots()
                return {"success": True, "message": f"{bot_type.capitalize()} Bot {symbol} stopped"}
        # bot_N short/main: alter Stop-Pfad bleibt aktiv
        elif prof and _is_registry_bot_profile(prof):
            await asyncio.to_thread(_stop_script_bot, symbol.strip().upper(), bot_type, prof)
            await asyncio.sleep(0.2)
            if not await asyncio.to_thread(is_bot_running, symbol, bot_type, prof):
                send_bot_alert(symbol, "stopped", f"{bot_type.capitalize()} Bot {symbol} ({prof}) wurde gestoppt")
                _stop_hedge_guardian_if_no_bots()
                return {"success": True, "message": f"{bot_type.capitalize()} Bot {symbol} stopped"}
            # Fallback: pkill wenn PID-Datei fehlte oder Prozess reagiert nicht
        else:
            # Main: systemctl versuchen
            result = await systemctl_stop_async(service_name, timeout=5)
            if result.returncode == 0:
                await asyncio.sleep(0.2)
                if not await asyncio.to_thread(is_bot_running, symbol, bot_type, prof):
                    send_bot_alert(symbol, "stopped", f"{bot_type.capitalize()} Bot {symbol} wurde gestoppt")
                    return {"success": True, "message": f"{bot_type.capitalize()} Bot {symbol} stopped"}
                logger.warning(
                    f"[BOT-STOP] systemctl stop returned 0, but process still running for {bot_type}@{symbol} -> fallback stop"
                )
        
        # Fallback 1: script-based PID stop (data/run/{long|short}_bot_SYMBOL[_bot_1].pid)
        prof_suffix = f"_{prof}" if _is_registry_bot_profile(prof) else ""
        try:
            safe_symbol = ''.join(ch if (ch.isalnum() or ch in "_-") else '_' for ch in str(symbol))
            run_dir = project_root / "data" / "run"
            pid_file = run_dir / f"{bot_type}_bot_{safe_symbol}{prof_suffix}.pid"
            if pid_file.exists():
                pid_raw = pid_file.read_text(encoding="utf-8").strip()
                if pid_raw.isdigit():
                    pid = int(pid_raw)
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    except Exception as exc:
                        logger.warning(f"[BOT-STOP] SIGTERM fehlgeschlagen für PID {pid}: {exc}")

                    # Schnellerer PID-Check: weniger Iterationen, kürzere Pausen
                    for _ in range(10):
                        try:
                            os.kill(pid, 0)
                            await asyncio.sleep(0.15)
                        except ProcessLookupError:
                            break
                        except Exception:
                            break
                    else:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except Exception:
                            pass

                    try:
                        if not pid_file.exists():
                            pass
                        else:
                            pid_file.unlink(missing_ok=True)
                    except Exception:
                        pass

                    await asyncio.sleep(0.2)
                    if not await asyncio.to_thread(is_bot_running, symbol, bot_type, prof):
                        send_bot_alert(symbol, "stopped", f"{bot_type.capitalize()} Bot {symbol} wurde gestoppt (PID fallback)")
                        _stop_hedge_guardian_if_no_bots()
                        return {"success": True, "message": f"{bot_type.capitalize()} Bot {symbol} stopped (pid)"}
        except Exception as exc:
            logger.error(f"[BOT-STOP] PID fallback stop failed for {bot_type}@{symbol}: {exc}", exc_info=True)

        # Fallback 2: kill by cmdline pattern (covers stray processes without PID file)
        try:
            script_name = f"{bot_type}_bot.py"
            await asyncio.to_thread(
                lambda: subprocess.run(
                    ['pkill', '-f', f"{script_name} {symbol}"],
                    capture_output=True, text=True, timeout=5
                )
            )
        except Exception:
            pass

        await asyncio.sleep(0.2)
        if not await asyncio.to_thread(is_bot_running, symbol, bot_type, prof):
            send_bot_alert(symbol, "stopped", f"{bot_type.capitalize()} Bot {symbol} wurde gestoppt (process fallback)")
            _stop_hedge_guardian_if_no_bots()
            return {"success": True, "message": f"{bot_type.capitalize()} Bot {symbol} stopped (process)"}

        err_msg = "Stop fehlgeschlagen (Prozess läuft weiter)"
        return {"success": False, "message": err_msg}
    except Exception as e:
        return {"success": False, "message": str(e)}


def backup_bot_logs(symbol: str, bot_type: str = "long") -> tuple[bool, str]:
    """
    Erstellt ein Backup der Bot-Logs vor dem Neustart.
    Returns: (success: bool, message: str)
    """
    try:
        # Erstelle backups-Verzeichnis falls nicht vorhanden
        project_root = Path(__file__).parent.parent
        logs_dir = project_root / "logs"
        backups_dir = project_root / "logs" / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        
        # Timestamp für Backup-Dateinamen
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if bot_type == "short":
            log_file = logs_dir / f"short_bot_{symbol}.log"
            backup_filename = f"short_bot_{symbol}_{timestamp}.log"
        else:
            log_file = logs_dir / f"long_bot_{symbol}.log"
            backup_filename = f"long_bot_{symbol}_{timestamp}.log"
        
        if not log_file.exists():
            return False, f"Log-Datei nicht gefunden: {log_file}"
        
        # Backup-Dateiname mit Timestamp
        backup_path = backups_dir / backup_filename
        
        # Kopiere Log-Datei
        shutil.copy2(log_file, backup_path)
        
        # Optional: Komprimiere alte Backups (nur wenn sie größer als 10MB sind)
        if backup_path.stat().st_size > 10 * 1024 * 1024:  # 10MB
            # Komprimiere das Backup
            import gzip
            with open(backup_path, 'rb') as f_in:
                with gzip.open(f"{backup_path}.gz", 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
            backup_path.unlink()  # Lösche unkomprimierte Datei
            backup_filename = f"{backup_filename}.gz"
        
        return True, f"Log-Backup erstellt: {backup_filename}"
    except Exception as e:
        return False, f"Fehler beim Erstellen des Log-Backups: {str(e)}"


@app.post("/api/bots/{symbol}/restart")
async def api_bot_restart(symbol: str, request: BotActionRequest, user: dict = Depends(require_auth)):
    """Restart bot with log backup"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        bot_type = request.bot_type

        # (C) Block restart if per-symbol config is missing.
        cfg_path = get_config_path(bot_type=bot_type, symbol=symbol)
        if not cfg_path.exists():
            return {
                "success": False,
                "message": f"Config fehlt für {bot_type}@{symbol}: {cfg_path}. Bitte erst im Dashboard speichern/anlegen.",
                "error_code": "MISSING_SYMBOL_CONFIG",
                "config_path": str(cfg_path),
            }

        # Schritt 1: Erstelle Log-Backup vor dem Neustart
        backup_success, backup_message = backup_bot_logs(symbol, bot_type=bot_type)
        if not backup_success:
            # Warnung, aber nicht kritisch - Neustart trotzdem versuchen
            logger.warning(f"⚠️ Log-Backup fehlgeschlagen: {backup_message}")
        
        # Schritt 2: Restarte Bot
        service_name = f'hedgebot-{bot_type}@{symbol}'
        result = run_sudo_command(
            ['sudo', 'systemctl', 'restart', service_name],
            timeout=10
        )
        
        if result.returncode == 0:
            message = f"{bot_type.capitalize()} Bot {symbol} restarted"
            if backup_success:
                message += f" | {backup_message}"
            else:
                message += f" | ⚠️ Backup-Fehler: {backup_message}"
            # Send alert
            send_bot_alert(symbol, "restarted", f"{bot_type.capitalize()} Bot {symbol} wurde neu gestartet")
            return {"success": True, "message": message}
        return {"success": False, "message": result.stderr}
    except Exception as e:
        return {"success": False, "message": str(e)}


def _safe_default_config(bot_type: str):
    """Return default config dict; never raise."""
    try:
        return get_default_config(bot_type=bot_type)
    except Exception:
        return {}


@app.get("/api/bots/{symbol}/config")
async def api_bot_config(symbol: str, user: dict = Depends(require_auth), bot_type: str = Query("long"), profile: Optional[str] = Query(None)):
    """Get bot config"""
    try:
        prof = (profile or "").strip().lower() or None
        config = load_config(symbol=symbol, bot_type=bot_type, fallback_to_global=True, profile=prof)
        if not config or config == {}:
            default_config = _safe_default_config(bot_type)
            return {"config": default_config, "warning": "Using default config - file not found"}
        
        if not isinstance(config, dict):
            default_config = _safe_default_config(bot_type)
            return {"config": default_config, "warning": "Invalid config format - using defaults"}
        
        return {"config": config}
    except FileNotFoundError:
        default_config = _safe_default_config(bot_type)
        return {"config": default_config, "warning": "Config file not found - using defaults"}
    except Exception as e:
        logger.error(f"Error loading config for {symbol}: {e}", exc_info=True)
        default_config = _safe_default_config(bot_type)
        return {"config": default_config, "error": str(e)}


@app.post("/api/bots/{symbol}/config")
async def api_bot_config_update(symbol: str, config: dict = Body(...), user: dict = Depends(require_auth), bot_type: str = Query("long")):
    """Update bot config"""
    logger.debug(f"[API] POST /api/bots/{symbol}/config - Request gestartet (bot_type: {bot_type})")
    logger.debug(f"[API] Config-Daten: {config}")
    
    if user.get("role") != "admin":
        logger.warning(f"[API] Zugriff verweigert - User ist nicht Admin: {user.get('username', 'unknown')}")
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Map alte Keys → neue (nur initial_long_usdt / initial_short_usdt)
        cfg = dict(config)
        if bot_type == "long":
            if "target_long_notional" in cfg and "initial_long_usdt" not in cfg:
                cfg["initial_long_usdt"] = cfg.pop("target_long_notional", None)
            cfg.pop("initial_long_notional", None)
            cfg.pop("target_long_notional", None)
        else:
            if "target_short_notional" in cfg and "initial_short_usdt" not in cfg:
                cfg["initial_short_usdt"] = cfg.pop("target_short_notional", None)
            cfg.pop("initial_short_notional", None)
            cfg.pop("target_short_notional", None)
        logger.info(f"💾 Speichere Config für {bot_type} bot ({symbol}): {cfg}")
        save_start = time.time()
        # Explicit save should create the per-symbol config if missing (A).
        prof = (config.get("profile") or "").strip().lower() or None
        cfg.pop("profile", None)  # Nicht in YAML speichern
        if save_config(symbol, cfg, bot_type=bot_type, create_if_missing=True, profile=prof):
            save_duration = time.time() - save_start
            logger.info(f"✅ Config erfolgreich gespeichert für {bot_type} bot ({symbol}) in {save_duration:.2f}s")
            logger.debug(f"[API] POST /api/bots/{symbol}/config - Erfolgreich abgeschlossen")
            return {"success": True, "message": f"Config for {bot_type} bot {symbol} updated"}
        save_duration = time.time() - save_start
        logger.error(f"❌ Fehler beim Speichern der Config für {bot_type} bot ({symbol}) nach {save_duration:.2f}s")
        return {"success": False, "message": "Failed to save config"}
    except Exception as e:
        save_duration = time.time() - save_start if 'save_start' in locals() else 0
        logger.error(f"❌ Exception beim Speichern der Config für {bot_type} bot ({symbol}) nach {save_duration:.2f}s: {e}", exc_info=True)
        return {"success": False, "message": str(e)}


# Service Management Endpoints
@app.post("/api/services/{service_key}/start")
async def api_service_start(service_key: str, user: dict = Depends(require_auth)):
    """Start a system service"""
    service_map = {
        "master": "hedgebot-master.service",
        "dashboard": "hedgebot-dashboard.service"
    }
    
    if service_key not in service_map:
        raise HTTPException(status_code=400, detail="Ungültiger Service-Key")
    
    service_name = service_map[service_key]
    
    try:
        result = run_sudo_command(
            ['sudo', 'systemctl', 'start', service_name],
            timeout=10
        )
        if result.returncode == 0:
            return {"success": True, "message": f"Service {service_name} wurde gestartet"}
        else:
            return {"success": False, "message": f"Fehler: {result.stderr}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/alerts/test")
async def api_test_alert(user: dict = Depends(require_auth)):
    """Test alert"""
    try:
        success = send_ntfy_alert(
            "🔔 Test-Nachricht vom Dashboard - Handy sollte klingeln!",
            title="Test Alert",
            priority="urgent",
            tags=["test_tube", "bell"]
        )
        if success:
            return {"success": True, "message": "Test-Alert gesendet"}
        else:
            return {"success": False, "message": "Alert konnte nicht gesendet werden (ntfy_topic nicht konfiguriert?)"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/services/{service_key}/stop")
async def api_service_stop(service_key: str, user: dict = Depends(require_auth)):
    """Stop a system service"""
    service_map = {
        "master": "hedgebot-master.service",
        "dashboard": "hedgebot-dashboard.service"
    }
    
    if service_key not in service_map:
        raise HTTPException(status_code=400, detail="Ungültiger Service-Key")
    
    service_name = service_map[service_key]
    
    try:
        result = run_sudo_command(
            ['sudo', 'systemctl', 'stop', service_name],
            timeout=10
        )
        if result.returncode == 0:
            return {"success": True, "message": f"Service {service_name} wurde gestoppt"}
        else:
            return {"success": False, "message": f"Fehler: {result.stderr}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _stop_hedge_guardian_if_no_bots() -> None:
    """Stoppt Main- bzw. Sub-Guardian, wenn kein Long- bzw. Short-Bot mehr läuft."""
    if not is_any_bot_running("long"):
        _stop_hedge_guardian("main")
    if not is_any_bot_running("short"):
        _stop_hedge_guardian("sub")


def _stop_hedge_guardian(scope: str = "both") -> None:
    """Stoppt Hedge-Guardian-Prozess(e): scope in ('main', 'sub', 'both')."""
    try:
        if scope == "both":
            subprocess.run(["pkill", "-f", "hedge_guardian.py"], capture_output=True, timeout=5)
        elif scope == "main":
            subprocess.run(["pkill", "-f", "hedge_guardian.py --account-scope main"], capture_output=True, timeout=5)
        elif scope == "sub":
            subprocess.run(["pkill", "-f", "hedge_guardian.py --account-scope sub"], capture_output=True, timeout=5)
    except Exception as e:
        logger.warning("Hedge Guardian Stop (%s): %s", scope, e)


def _start_hedge_guardian_after_bots_sync(scope: str = "both", symbol: str | None = None) -> None:
    """Startet start_guardians_after_bots.sh mit Scope (main|sub|both) und optional Symbol für diesen Coin."""
    _project_root = Path(__file__).resolve().parent.parent
    script = _project_root / "start_guardians_after_bots.sh"
    if not script.exists():
        return
    try:
        bash_cmd = shutil.which("bash") or "bash"
        cmd = [bash_cmd, str(script), scope]
        if symbol and str(symbol).strip().upper():
            cmd.append(str(symbol).strip().upper())
        subprocess.run(
            cmd,
            cwd=str(_project_root),
            capture_output=True,
            timeout=45,
            env={**os.environ, "PYTHONPATH": str(_project_root)},
        )
    except Exception as e:
        logger.warning("Hedge Guardian Start (scope=%s, symbol=%s): %s", scope, symbol, e)


def _start_hedge_guardian_after_bots_async(scope: str = "both", symbol: str | None = None) -> None:
    """Startet Hedge Guardian im Hintergrund mit Scope und optional Symbol (für diesen Coin)."""
    try:
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, lambda: _start_hedge_guardian_after_bots_sync(scope, symbol))
    except Exception as e:
        logger.warning("Hedge Guardian async Start: %s", e)


@app.post("/api/hedge-guardian/stop")
async def api_hedge_guardian_stop(body: dict = Body(default={}), user: dict = Depends(require_auth)):
    """Stoppt den Hedge Guardian für einen Scope (main|sub)."""
    scope = (body.get("scope") or "").strip().lower()
    if scope not in ("main", "sub"):
        return {"success": False, "message": "scope muss 'main' oder 'sub' sein"}
    try:
        _stop_hedge_guardian(scope)
        return {"success": True, "scope": scope, "message": f"Hedge Guardian ({scope}) gestoppt"}
    except Exception as e:
        logger.exception("Hedge Guardian Stop: %s", e)
        return {"success": False, "message": str(e)}


@app.post("/api/hedge-guardian/start")
async def api_hedge_guardian_start(body: dict = Body(default={}), user: dict = Depends(require_auth)):
    """Startet den Hedge Guardian für einen Scope (main|sub) mit optionalem Symbol."""
    scope = (body.get("scope") or "").strip().lower()
    if scope not in ("main", "sub"):
        return {"success": False, "message": "scope muss 'main' oder 'sub' sein"}
    symbol = (body.get("symbol") or "").strip().upper() or None
    try:
        _start_hedge_guardian_after_bots_async(scope, symbol=symbol)
        return {"success": True, "scope": scope, "symbol": symbol, "message": f"Hedge Guardian ({scope}) wird gestartet"}
    except Exception as e:
        logger.exception("Hedge Guardian Start: %s", e)
        return {"success": False, "message": str(e)}


def _read_watcher_info(pid_file: Path, keyword: str) -> dict[str, Any]:
    info = {"status": "stopped", "pid": None}
    if not pid_file.exists():
        return info
    try:
        pid = int(pid_file.read_text().strip())
    except Exception:
        pid_file.unlink(missing_ok=True)
        return info
    proc_path = Path("/proc") / str(pid)
    if not proc_path.exists():
        pid_file.unlink(missing_ok=True)
        return info
    try:
        cmdline = proc_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
    except Exception:
        pid_file.unlink(missing_ok=True)
        return info
    if keyword not in cmdline:
        pid_file.unlink(missing_ok=True)
        return info
    info["status"] = "running"
    info["pid"] = pid
    return info


def _read_status_file() -> dict[str, Any] | None:
    if not WATCHERS_STATUS_FILE.exists():
        return None
    try:
        data = json.loads(WATCHERS_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    watchers = data.get("watchers") or {}
    if not watchers:
        return None
    return {
        "status": data.get("status") or "stopped",
        "watchers": {
            "safety_order_watchdog": watchers.get("safety_order_watchdog", {"status": "stopped", "pid": None}),
            "wallet_refill_watchdog": watchers.get("wallet_refill_watchdog", {"status": "stopped", "pid": None}),
        },
        "ok": True,
    }


def _hedge_guard_watchers_status() -> dict[str, Any]:
    status_data = _read_status_file()
    if status_data:
        return status_data
    safety = _read_watcher_info(LIVE_BOT_LOGS_ROOT / "run" / "safety_order_watchdog.pid", "safety_order_watchdog.py")
    wallet = _read_watcher_info(LIVE_BOT_LOGS_ROOT / "run" / "wallet_refill_watchdog.pid", "wallet_refill_watchdog.py")
    running = sum(1 for entry in (safety, wallet) if entry["status"] == "running")
    overall = "stopped"
    if running == 2:
        overall = "running"
    elif running == 1:
        overall = "partial"
    return {"ok": True, "status": overall, "watchers": {"safety_order_watchdog": safety, "wallet_refill_watchdog": wallet}}


@app.get("/api/hedge-guard-watchers/status")
def api_hedge_guard_watchers_status():
    return _hedge_guard_watchers_status()


def _run_hedge_guard_script(script_path: Path) -> dict[str, Any]:
    if not script_path.exists():
        return {"ok": False, "error": f"script missing: {script_path}"}
    try:
        subprocess.run([str(script_path)], cwd=str(project_root), check=True)
        return _hedge_guard_watchers_status()
    except subprocess.CalledProcessError as exc:
        logger.error("Hedge guard watcher script failed: %s", exc)
        return {"ok": False, "error": str(exc)}


@app.post("/api/hedge-guard-watchers/start")
def api_hedge_guard_watchers_start():
    return _run_hedge_guard_script(START_WATCHERS_SCRIPT)


@app.post("/api/hedge-guard-watchers/stop")
def api_hedge_guard_watchers_stop():
    return _run_hedge_guard_script(STOP_WATCHERS_SCRIPT)


@app.post("/api/services/{service_key}/restart")
async def api_service_restart(service_key: str, user: dict = Depends(require_auth)):
    """Restart a system service (dashboard) or start/restart Hedge Guard (script). Master Bot nicht mehr genutzt."""
    service_map = {
        "dashboard": "hedgebot-dashboard.service"
    }

    # Hedge Guard: kein systemd, Script start_guardians_after_bots.sh (stoppt ggf. zuerst)
    if service_key == "hedge_guard":
        try:
            _project_root = Path(__file__).resolve().parent.parent
            script = _project_root / "start_guardians_after_bots.sh"
            if not script.exists():
                return {"success": False, "message": f"Script nicht gefunden: {script}"}
            bash_cmd = shutil.which("bash") or "bash"
            # Bestehende Guardian-Prozesse beenden (echter Restart)
            subprocess.run(
                ["pkill", "-f", "hedge_guardian.py"],
                cwd=str(_project_root),
                capture_output=True,
                timeout=5,
            )
            time.sleep(2)
            # Script im Hintergrund starten mit Scope "both" (beide Guardian-Prozesse)
            subprocess.Popen(
                [bash_cmd, str(script), "both"],
                cwd=str(_project_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "PYTHONPATH": str(_project_root)},
            )
            return {"success": True, "message": "Hedge Guard wird neu gestartet (ca. 10s Verzögerung, dann starten die Guardian-Prozesse)."}
        except Exception as e:
            logger.exception("Hedge Guard Restart: %s", e)
            return {"success": False, "message": str(e)}

    if service_key not in service_map:
        raise HTTPException(status_code=400, detail="Ungültiger Service-Key")

    service_name = service_map[service_key]

    try:
        result = run_sudo_command(
            ["sudo", "systemctl", "restart", service_name],
            timeout=10
        )
        if result.returncode == 0:
            return {"success": True, "message": f"Service {service_name} wurde neu gestartet"}
        return {"success": False, "message": result.stderr or "Unbekannter Fehler"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/system/restart-all")
async def api_restart_all_services(user: dict = Depends(require_auth)):
    """
    Restartet den Master Bot-Dienst neu. Dashboard bleibt aktiv.
    """
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    service_name = "hedgebot-master.service"
    steps = []
    errors = []

    try:
        steps.append(f"Starte {service_name} neu")
        result = run_sudo_command(['sudo', 'systemctl', 'restart', service_name], timeout=30)

        if result.returncode == 0:
            steps.append(f"{service_name} erfolgreich neu gestartet")
            return {
                "success": True,
                "steps": steps,
                "errors": [],
                "stopped_bots": []
            }

        error_message = result.stderr or result.stdout or "Unbekannter Fehler"
        logger.error(f"[RESTART-ALL] {service_name} restart failed: {error_message.strip()}")
        errors.append(error_message.strip())
        return {
            "success": False,
            "steps": steps,
            "errors": errors,
            "stopped_bots": []
        }

    except Exception as e:
        logger.error(f"[RESTART-ALL] Fehler beim Neustart von {service_name}: {e}", exc_info=True)
        errors.append(str(e))
        return {
            "success": False,
            "steps": steps,
            "errors": errors,
            "stopped_bots": []
        }

@app.get("/api/system/pre-flight-check")
async def api_pre_flight_check(user: dict = Depends(require_auth)):
    """
    Pre-Flight-Check: Prüft alle kritischen Services vor dem Setzen von Orders.
    Sollte VOR jeder Trading-Aktion aufgerufen werden.
    """
    try:
        checks = {
            "master_bot": {"status": "unknown", "message": "", "critical": True},
            "master_bot_api": {"status": "unknown", "message": "", "critical": True},
            "duplicate_processes": {"status": "unknown", "message": "", "critical": True},
            "dashboard": {"status": "unknown", "message": "", "critical": False}
        }
        
        all_checks_passed = True
        
        # 1. Prüfe Master Bot
        try:
            master_running = is_master_bot_running()
            if master_running:
                checks["master_bot"]["status"] = "ok"
                checks["master_bot"]["message"] = "Master Bot läuft"
            else:
                checks["master_bot"]["status"] = "error"
                checks["master_bot"]["message"] = "Master Bot läuft NICHT - bitte starten!"
                all_checks_passed = False
        except Exception as e:
            checks["master_bot"]["status"] = "error"
            checks["master_bot"]["message"] = f"Fehler beim Prüfen: {str(e)}"
            all_checks_passed = False
        
        # 3. Prüfe Master Bot API Erreichbarkeit
        try:
            # Prüfe zuerst, ob der Prozess läuft
            api_process_running = is_master_bot_api_running()
            
            if not api_process_running:
                # Prozess läuft nicht - versuche zu starten
                logger.warning("[PRE-FLIGHT] Master Bot API Prozess läuft nicht - versuche zu starten...")
                try:
                    project_root = Path(__file__).parent.parent
                    api_script = project_root / "bots" / "master_bot_api.py"
                    venv_python = project_root / "venv" / "bin" / "python3"
                    
                    if venv_python.exists():
                        python_cmd = str(venv_python)
                    else:
                        python_cmd = "python3"
                    
                    # Starte Master Bot API im Hintergrund
                    result = subprocess.Popen(
                        [python_cmd, str(api_script)],
                        cwd=str(project_root),
                        env={**os.environ, "PYTHONPATH": str(project_root)},
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True
                    )
                    time.sleep(3)  # Warte, damit Prozess vollständig startet
                    
                    # Prüfe erneut
                    api_process_running = is_master_bot_api_running()
                    if api_process_running:
                        started_pids = find_all_master_bot_api_processes()
                        logger.info(f"[PRE-FLIGHT] ✅ Master Bot API erfolgreich gestartet (PID: {started_pids[0] if started_pids else 'unknown'})")
                    else:
                        logger.error("[PRE-FLIGHT] ❌ Master Bot API konnte nicht gestartet werden")
                except Exception as start_error:
                    logger.error(f"[PRE-FLIGHT] ❌ Fehler beim Starten der Master Bot API: {start_error}", exc_info=True)
            
            # Prüfe Erreichbarkeit über HTTP
            request_id = str(uuid.uuid4())
            response = httpx.get(
                f"{MASTER_BOT_API_URL}/master/health",
                headers={
                    "X-Request-ID": request_id,
                    "X-Internal-Token": MASTER_BOT_API_TOKEN
                },
                timeout=10.0  # Erhöhtes Timeout, da /master/positions Requests die API blockieren können
            )
            if response.status_code == 200:
                checks["master_bot_api"]["status"] = "ok"
                checks["master_bot_api"]["message"] = "Master Bot API erreichbar"
            else:
                checks["master_bot_api"]["status"] = "error"
                checks["master_bot_api"]["message"] = f"Master Bot API antwortet mit Status {response.status_code}"
                all_checks_passed = False
        except httpx.TimeoutException:
            checks["master_bot_api"]["status"] = "error"
            checks["master_bot_api"]["message"] = "Master Bot API nicht erreichbar (Timeout)"
            all_checks_passed = False
        except httpx.ConnectError as e:
            # Connection refused - API läuft nicht oder ist nicht erreichbar
            checks["master_bot_api"]["status"] = "error"
            checks["master_bot_api"]["message"] = f"Master Bot API nicht erreichbar (Connection refused) - Prozess läuft möglicherweise nicht"
            all_checks_passed = False
        except Exception as e:
            checks["master_bot_api"]["status"] = "error"
            checks["master_bot_api"]["message"] = f"Fehler beim Prüfen: {str(e)}"
            all_checks_passed = False
        
        # 4. Prüfe auf doppelte Prozesse
        try:
            import psutil
            master_bot_processes = []
            
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = proc.info.get('cmdline', [])
                    if cmdline:
                        cmdline_str = ' '.join(str(arg) for arg in cmdline)
                        if 'master_bot.py' in cmdline_str and 'master_bot_api.py' not in cmdline_str:
                            master_bot_processes.append(proc.info['pid'])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            duplicate_issues = []
            if len(master_bot_processes) > 1:
                duplicate_issues.append(f"Master Bot: {len(master_bot_processes)} Prozesse (PIDs: {master_bot_processes})")
            
            if duplicate_issues:
                checks["duplicate_processes"]["status"] = "error"
                checks["duplicate_processes"]["message"] = "Doppelte Prozesse gefunden: " + ", ".join(duplicate_issues)
                all_checks_passed = False
            else:
                checks["duplicate_processes"]["status"] = "ok"
                checks["duplicate_processes"]["message"] = "Keine doppelten Prozesse gefunden"
        except ImportError:
            checks["duplicate_processes"]["status"] = "warning"
            checks["duplicate_processes"]["message"] = "psutil nicht verfügbar - kann nicht prüfen"
        except Exception as e:
            checks["duplicate_processes"]["status"] = "warning"
            checks["duplicate_processes"]["message"] = f"Fehler beim Prüfen: {str(e)}"
        
        # 5. Prüfe Dashboard (optional)
        try:
            checks["dashboard"]["status"] = "ok"
            checks["dashboard"]["message"] = "Dashboard läuft"
        except Exception as e:
            checks["dashboard"]["status"] = "warning"
            checks["dashboard"]["message"] = f"Fehler beim Prüfen: {str(e)}"
        
        # Zusammenfassung
        summary = {
            "all_checks_passed": all_checks_passed,
            "critical_checks_passed": all(
                checks[key]["status"] == "ok" 
                for key, check in checks.items() 
                if check.get("critical", False)
            ),
            "checks": checks,
            "recommendation": "✅ System bereit für Trading-Aktionen" if all_checks_passed else "❌ System NICHT bereit - bitte Probleme beheben"
        }
        
        return JSONResponse(summary)
    except Exception as e:
        logger.error(f"Fehler beim Pre-Flight-Check: {e}", exc_info=True)
        return JSONResponse({
            "all_checks_passed": False,
            "critical_checks_passed": False,
            "checks": {},
            "recommendation": f"❌ Fehler beim Pre-Flight-Check: {str(e)}",
            "error": str(e)
        }, status_code=500)


@app.get("/api/system/processes")
async def api_get_system_processes(user: dict = Depends(require_auth)):
    """Laufende Prozesse wie im Terminal: `ps aux | grep .py` (inkl. grep selbst)."""
    try:
        # Use bash -lc so the pipeline behaves exactly like in an interactive shell.
        result = subprocess.run(
            ["bash", "-lc", "ps aux | grep .py"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode not in (0, 1):
            # grep returns 1 when no matches; that's not an error for us.
            err = (result.stderr or "").strip() or f"exit_code={result.returncode}"
            return {"success": False, "error": err, "output": ""}
        output = (result.stdout or "").rstrip("\n")
        if not output.strip():
            output = "(keine Treffer für: ps aux | grep .py)"
        return {"success": True, "output": output}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Timeout (ps aux)", "output": ""}
    except Exception as e:
        logger.error(f"Fehler beim Abrufen der Prozesse: {e}", exc_info=True)
        return {"success": False, "error": str(e), "output": ""}


@app.get("/api/system/status")
async def api_get_system_status(
    user: dict = Depends(require_auth),
    profile: Optional[str] = Query(None, description="main|bot_N – bei bot_N: price_at kann leer sein"),
):
    """Get status of all system processes (Master Bot, Dashboard, and all bot instances)"""
    # Reset circuit breaker for this endpoint (new endpoint, shouldn't be blocked)
    endpoint = "GET /api/system/status"
    if endpoint in circuit_breaker:
        circuit_breaker[endpoint] = {'failures': 0, 'last_failure': 0, 'state': 'closed'}
        logger.debug(f"[SYSTEM-STATUS] Circuit Breaker für {endpoint} zurückgesetzt")
    
    try:
        logger.debug("[SYSTEM-STATUS] Starte Status-Abfrage...")
        profile_long_bot_status = None
        
        # Get all services status (Master Bot, Dashboard)
        try:
            services_status = get_all_services_status()
            logger.debug(f"[SYSTEM-STATUS] Services-Status abgerufen: {services_status}")
        except Exception as e:
            logger.error(f"[SYSTEM-STATUS] Fehler beim Abrufen des Services-Status: {e}", exc_info=True)
            services_status = {"services": {}, "all_active": False}
        
        # Get all bot instances (running AND inactive)
        try:
            all_bots = get_all_bots(include_inactive=True)
            logger.debug(f"[SYSTEM-STATUS] Bot-Liste abgerufen: {len(all_bots)} Bots (inkl. inaktive)")
        except Exception as e:
            logger.warning(f"[SYSTEM-STATUS] Fehler beim Abrufen der Bot-Liste: {e}", exc_info=True)
            all_bots = []
        
        # Group bots by symbol (including inactive ones)
        bots_by_symbol = {}
        for bot in all_bots:
            try:
                symbol = bot.get("symbol", "UNKNOWN")
                bot_type = bot.get("bot_type", "long")
                if symbol not in bots_by_symbol:
                    bots_by_symbol[symbol] = {}
                bot_entry = dict(bot)
                if bot_entry.get("status_label") and not bot_entry.get("status_text"):
                    bot_entry["status_text"] = bot_entry.get("status_label")
                bots_by_symbol[symbol][bot_type] = bot_entry
            except Exception as e:
                logger.warning(f"[SYSTEM-STATUS] Fehler beim Verarbeiten von Bot {bot}: {e}")
                continue
        
        # Also check PID file for local mode bots (even if not running)
        try:
            project_root = Path(__file__).parent.parent
            pid_file = project_root / "data" / "logs" / "local_bots_pids.json"
            if pid_file.exists():
                with open(pid_file, 'r') as f:
                    pids_dict = json.load(f)
                
                for bot_key, pid in pids_dict.items():
                    # bot_key format: "long_SYMBOL" or "short_SYMBOL"
                    parts = bot_key.split('_', 1)
                    if len(parts) == 2:
                        bot_type_key, symbol = parts
                        if symbol not in bots_by_symbol:
                            bots_by_symbol[symbol] = {}
                        
                        # Add bot if not already in list (or update if exists)
                        if bot_type_key not in bots_by_symbol[symbol]:
                            if bot_type_key == "long":
                                bots_by_symbol[symbol][bot_type_key] = _get_profile_scoped_long_status(symbol, profile=profile)
                            else:
                                from utils.bot_monitor import is_bot_running
                                bots_by_symbol[symbol][bot_type_key] = {
                                    "running": is_bot_running(symbol, bot_type=bot_type_key),
                                    "service_name": f"hedgebot-{bot_type_key}@{symbol}"
                                }
        except Exception as e:
            logger.debug(f"[SYSTEM-STATUS] Konnte PID-Datei nicht prüfen (optional): {e}")
        
        # Also extract symbols from log files (to show bots even if not running)
        try:
            project_root = Path(__file__).parent.parent
            bots_log_dir = project_root / "data" / "logs"
            logger.info(f"[SYSTEM-STATUS] START Log-Dateien-Extraktion: project_root={project_root}, bots_log_dir={bots_log_dir}, exists={bots_log_dir.exists()}")
            
            # Check log files in bots/logs for symbols
            if bots_log_dir.exists():
                log_files = list(bots_log_dir.glob("*_bot_*.log"))
                logger.debug(f"[SYSTEM-STATUS] Gefundene Log-Dateien: {len(log_files)}")
                for log_file in log_files:
                    # Extract symbol from filename: long_bot_SYMBOL.log or short_bot_SYMBOL.log
                    filename = log_file.stem
                    parts = filename.split('_')
                    logger.debug(f"[SYSTEM-STATUS] Verarbeite Log-Datei: {log_file.name}, Parts: {parts}")
                    if len(parts) >= 3 and parts[0] in ['long', 'short']:
                        bot_type_key = parts[0]  # "long" or "short"
                        symbol = parts[-1]  # Last part is the symbol
                        logger.debug(f"[SYSTEM-STATUS] Extrahiert: {bot_type_key} Bot für {symbol}")
                        
                        if symbol not in bots_by_symbol:
                            bots_by_symbol[symbol] = {}
                        
                        if bot_type_key not in bots_by_symbol[symbol]:
                            if bot_type_key == "long":
                                bots_by_symbol[symbol][bot_type_key] = _get_profile_scoped_long_status(symbol, profile=profile)
                            else:
                                from utils.bot_monitor import is_bot_running
                                bots_by_symbol[symbol][bot_type_key] = {
                                    "running": is_bot_running(symbol, bot_type=bot_type_key),
                                    "service_name": f"hedgebot-{bot_type_key}@{symbol}"
                                }
                            logger.debug(f"[SYSTEM-STATUS] Bot hinzugefügt: {bot_type_key}@{symbol}, running={bots_by_symbol[symbol][bot_type_key]['running']}")
                logger.info(f"[SYSTEM-STATUS] Nach Log-Dateien: {len(bots_by_symbol)} Symbole mit Bots")
            
            # Check state files for symbols
            for state_file in project_root.glob("*bot_state_*.json"):
                # Extract symbol from filename: long_bot_state_SYMBOL.json or short_bot_state_SYMBOL.json
                filename = state_file.stem
                parts = filename.split('_')
                if len(parts) >= 3 and parts[0] in ['long', 'short']:
                    bot_type_key = parts[0]  # "long" or "short"
                    symbol = parts[-1]  # Last part is the symbol
                    
                    if symbol not in bots_by_symbol:
                        bots_by_symbol[symbol] = {}
                    
                    if bot_type_key not in bots_by_symbol[symbol]:
                        if bot_type_key == "long":
                            bots_by_symbol[symbol][bot_type_key] = _get_profile_scoped_long_status(symbol, profile=profile)
                        else:
                            from utils.bot_monitor import is_bot_running
                            bots_by_symbol[symbol][bot_type_key] = {
                                "running": is_bot_running(symbol, bot_type=bot_type_key),
                                "service_name": f"hedgebot-{bot_type_key}@{symbol}"
                            }
        except Exception as e:
            logger.error(f"[SYSTEM-STATUS] FEHLER beim Extrahieren von Symbolen aus Log/State-Dateien: {e}", exc_info=True)
        
        # Also check for symbols with positions (to show bots even if not running)
        try:
            # Get symbols with positions from Master Bot API
            request_id = str(uuid.uuid4())
            response = httpx.get(
                f"{MASTER_BOT_API_URL}/master/positions",
                headers={
                    "X-Request-ID": request_id,
                    "X-Internal-Token": MASTER_BOT_API_TOKEN
                },
                timeout=10.0
            )
            
            if response.status_code == 200:
                api_response = response.json()
                if api_response.get("success"):
                    data = api_response.get("data", {})
                    symbols_with_positions = data.get("symbols", [])
                    
                    # For each symbol with positions, ensure Long/Short Bot status is checked
                    for symbol in symbols_with_positions:
                        if symbol not in bots_by_symbol:
                            bots_by_symbol[symbol] = {}
                        
                        # Check Long Bot status (even if not running)
                        if 'long' not in bots_by_symbol[symbol]:
                            bots_by_symbol[symbol]['long'] = _get_profile_scoped_long_status(symbol, profile=profile)
                        
                        # Check Short Bot status (even if not running)
                        if 'short' not in bots_by_symbol[symbol]:
                            from utils.bot_monitor import is_bot_running
                            bots_by_symbol[symbol]['short'] = {
                                "running": is_bot_running(symbol, bot_type="short"),
                                "service_name": f"hedgebot-short@{symbol}"
                            }
        except Exception as e:
            logger.debug(f"[SYSTEM-STATUS] Konnte Symbole mit Positionen nicht abrufen (optional): {e}")

        prof = _normalize_dashboard_profile(profile, fallback_to_main=False)
        if prof and is_bot_profile(prof):
            try:
                bot_name = profile_to_long_bot_name(prof)
                if bot_name:
                    profile_long_bot_status = get_bot_status(
                        "",
                        bot_type="long",
                        bot_name=bot_name,
                        profile=prof,
                    )
                    if (
                        profile_long_bot_status.get("status_label")
                        and not profile_long_bot_status.get("status_text")
                    ):
                        profile_long_bot_status["status_text"] = profile_long_bot_status.get("status_label")
            except Exception as exc:
                logger.debug("[SYSTEM-STATUS] profile_long_bot_status konnte nicht geladen werden: %s", exc)
        
        _ensure_fixed_cycle_long_bot_status(bots_by_symbol, profile=profile)
        focus_symbol = _inject_profile_long_bot_runtime_status(bots_by_symbol, profile=profile)
        logger.info(f"[SYSTEM-STATUS] Finale bots_by_symbol vor Response: {len(bots_by_symbol)} Symbole, Details: {bots_by_symbol}")
        
        # Price-at (Start-Preis-Bot) State – profilspezifisch.
        prof_status = _normalize_dashboard_profile(profile, fallback_to_main=True) or "main"
        try:
            price_at = _load_price_at_state_raw(prof_status)
        except Exception as e:
            logger.debug(f"[SYSTEM-STATUS] price_at optional: {e}")
            price_at = {"long": {}, "short": {}}
        
        result = {
            "success": True,
            "services": services_status.get("services", {}),
            "all_services_active": services_status.get("all_active", False),
            "bots": bots_by_symbol,
            "total_bots": len(all_bots),
            "price_at": price_at,
            "focus_symbol": focus_symbol,
            "profile_long_bot_status": profile_long_bot_status,
        }
        
        logger.info(f"[SYSTEM-STATUS] Status-Abfrage erfolgreich: {len(result.get('services', {}))} Services, {len(bots_by_symbol)} Symbole mit Bots, {result.get('total_bots', 0)} laufende Bots")
        logger.info(f"[SYSTEM-STATUS] Bots by symbol (Details): {bots_by_symbol}")
        
        # Reset circuit breaker on success
        if endpoint in circuit_breaker:
            circuit_breaker[endpoint] = {'failures': 0, 'last_failure': 0, 'state': 'closed'}
        
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"[SYSTEM-STATUS] Unerwarteter Fehler beim Abrufen des System-Status: {e}", exc_info=True)
        
        # Don't increment circuit breaker failures for this endpoint (it's new and might have initial issues)
        # Just return the error response. price_at immer mitsenden, damit Start-Preis-Cards weiter angezeigt werden.
        prof_fb = _normalize_dashboard_profile(profile, fallback_to_main=True) or "main"
        try:
            price_at_fallback = _load_price_at_state_raw(prof_fb)
        except Exception:
            price_at_fallback = {"long": {}, "short": {}}
        
        return JSONResponse({
            "success": False,
            "error": str(e),
            "services": {},
            "bots": {},
            "all_services_active": False,
            "total_bots": 0,
            "price_at": price_at_fallback,
            "profile_long_bot_status": None,
        }, status_code=200)  # Status 200 statt 503, damit Frontend die Fehlermeldung anzeigen kann


@app.get("/api/system/all-bots-overview")
async def api_get_all_bots_overview(user: dict = Depends(require_auth)):
    """
    Liefert eine kompakte Übersicht aller Bots über alle Profile (main, bot_N).
    Für jeden Bot: Symbol, Profil, Long-Running, Short-Running.
    Nutzbar für "auf einen Blick" Dashboard-Widget.
    """
    try:
        from utils.bot_monitor import is_bot_running

        profiles = [(entry["profile"], entry["display_name"]) for entry in _get_dashboard_profile_entries()]
        result = {"success": True, "profiles": {}}

        for prof_key, prof_label in profiles:
            profile_value = prof_key if is_bot_profile(prof_key) else None
            symbols = _list_symbols_from_dropdown_config_sources(profile=profile_value)
            bots_list = []
            prof_for_monitor = profile_value
            for sym in symbols:
                long_running = bool(_get_profile_scoped_long_status(sym, profile=prof_for_monitor).get("running"))
                short_running = is_bot_running(sym, bot_type="short", profile=prof_for_monitor)
                bots_list.append({
                    "symbol": sym,
                    "long": long_running,
                    "short": short_running,
                })
            _inject_fixed_cycle_overview_entry(bots_list, prof_key)
            result["profiles"][prof_key] = {
                "label": prof_label,
                "bots": bots_list,
            }

        return JSONResponse(result)
    except Exception as e:
        logger.error(f"[ALL-BOTS-OVERVIEW] Fehler: {e}", exc_info=True)
        return JSONResponse(
            {"success": False, "error": str(e), "profiles": {}},
            status_code=200
        )


@app.post("/api/system/start/{service_name}")
async def api_start_service(service_name: str, user: dict = Depends(require_auth)):
    """Start a system service (master or bot instance)"""
    try:
        project_root = Path(__file__).parent.parent
        venv_python = project_root / "venv" / "bin" / "python"
        python_cmd = str(venv_python) if venv_python.exists() else "python3"
        
        if service_name == "master":
            # Prüfe ob Master Bot bereits läuft (verhindert Doppelstarts)
            from utils.bot_monitor import is_master_bot_running
            if is_master_bot_running():
                return {"success": False, "error": "Master Bot läuft bereits"}
            
            # Start Master Bot
            try:
                # Try systemd first
                result = subprocess.run(
                    ['sudo', '-S', 'systemctl', 'start', 'hedgebot-master.service'],
                    input=f'{get_sudo_password()}\n',
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    return {"success": True, "message": "Master Bot gestartet (systemd)"}
            except:
                pass
            
            # Fallback: Start as process
            master_bot_script = project_root / "bots" / "master_bot.py"
            process = subprocess.Popen(
                [python_cmd, str(master_bot_script)],
                cwd=str(project_root),
                env={**os.environ, "PYTHONPATH": str(project_root)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            return {"success": True, "message": f"Master Bot gestartet (PID: {process.pid})"}
        
        else:
            return {"success": False, "error": f"Unbekannter Service: {service_name}"}
    
    except Exception as e:
        logger.error(f"Error starting service {service_name}: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.post("/api/system/stop/{service_name}")
async def api_stop_service(service_name: str, user: dict = Depends(require_auth)):
    """Stop a system service"""
    try:
        if service_name == "master":
            # WICHTIG: Stoppe zuerst ALLE getrackten Bot-Prozesse aus der PID-Datei
            try:
                import psutil
                pid_file = "logs/local_bots_pids.json"
                
                # Lade PIDs aus der Datei
                pids_dict = {}
                if os.path.exists(pid_file):
                    try:
                        with open(pid_file, 'r') as f:
                            pids_dict = json.load(f)
                    except Exception as e:
                        logger.warning(f"⚠️ Konnte PID-Datei nicht laden: {e}")
                
                if pids_dict:
                    logger.info(f"🛑 Stoppe {len(pids_dict)} getrackte Bot-Prozess(e) aus PID-Datei: {list(pids_dict.keys())}")
                    stopped_count = 0
                    for bot_key, pid in pids_dict.items():
                        try:
                            # Prüfe ob Prozess noch läuft
                            try:
                                process = psutil.Process(pid)
                                if process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                                    logger.debug(f"ℹ️ Bot-Prozess {bot_key} (PID: {pid}) ist bereits Zombie/Dead")
                                    continue
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                logger.debug(f"ℹ️ Bot-Prozess {bot_key} (PID: {pid}) läuft nicht mehr")
                                continue
                            
                            # Stoppe Prozess
                            process = psutil.Process(pid)
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                                logger.info(f"✅ Bot-Prozess {bot_key} (PID: {pid}) gestoppt")
                                stopped_count += 1
                            except psutil.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=2)
                                logger.info(f"✅ Bot-Prozess {bot_key} (PID: {pid}) mit SIGKILL gestoppt")
                                stopped_count += 1
                        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                            logger.debug(f"ℹ️ Bot-Prozess {bot_key} (PID: {pid}) konnte nicht gestoppt werden: {e}")
                        except Exception as e:
                            logger.warning(f"⚠️ Fehler beim Stoppen von Bot-Prozess {bot_key} (PID: {pid}): {e}")
                    
                    # Leere die PID-Datei nach dem Stoppen
                    if stopped_count > 0:
                        try:
                            with open(pid_file, 'w') as f:
                                json.dump({}, f)
                            logger.info(f"✅ {stopped_count} Bot-Prozess(e) gestoppt und PID-Datei geleert")
                        except Exception as e:
                            logger.warning(f"⚠️ Konnte PID-Datei nicht leeren: {e}")
                    else:
                        logger.info("ℹ️ Keine laufenden Bot-Prozesse in PID-Datei gefunden")
                else:
                    logger.info("ℹ️ Keine getrackten Bot-Prozesse in PID-Datei gefunden")
            except ImportError:
                logger.warning("⚠️ psutil nicht verfügbar - kann Bot-Prozesse nicht stoppen")
            except Exception as e:
                logger.warning(f"⚠️ Fehler beim Stoppen der Bot-Prozesse: {e}")
            
            # Warte kurz, damit alle Prozesse beendet sind
            time.sleep(1)
            
            # Stop Master Bot - sowohl systemd als auch manuelle Prozesse
            master_bot_stopped = False
            
            # 1. Versuche systemd zu stoppen
            try:
                result = subprocess.run(
                    ['sudo', '-S', 'systemctl', 'stop', 'hedgebot-master.service'],
                    input=f'{get_sudo_password()}\n',
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    logger.info("✅ Master Bot gestoppt (systemd)")
                    master_bot_stopped = True
            except Exception as e:
                logger.debug(f"⚠️ systemd stop fehlgeschlagen: {e}")
            
            # 2. Stoppe ALLE Master Bot Prozesse (auch manuell gestartete)
            try:
                import psutil
                master_pids = []
                for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                    try:
                        cmdline = proc.info.get('cmdline', [])
                        if cmdline and len(cmdline) >= 2:
                            # Prüfe auf master_bot.py (nicht master_bot_api.py)
                            if 'master_bot.py' in ' '.join(cmdline) and 'master_bot_api.py' not in ' '.join(cmdline):
                                master_pids.append(proc.info['pid'])
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                
                if master_pids:
                    logger.info(f"🛑 Stoppe {len(master_pids)} Master Bot Prozess(e): {master_pids}")
                    for pid in master_pids:
                        try:
                            process = psutil.Process(pid)
                            if process.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                                process.terminate()
                                try:
                                    process.wait(timeout=5)
                                    logger.info(f"✅ Master Bot Prozess {pid} gestoppt")
                                    master_bot_stopped = True
                                except psutil.TimeoutExpired:
                                    process.kill()
                                    process.wait(timeout=2)
                                    logger.info(f"✅ Master Bot Prozess {pid} mit SIGKILL gestoppt")
                                    master_bot_stopped = True
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                        except Exception as e:
                            logger.warning(f"⚠️ Fehler beim Stoppen von Master Bot Prozess {pid}: {e}")
            except ImportError:
                logger.warning("⚠️ psutil nicht verfügbar - verwende pkill als Fallback")
            except Exception as e:
                logger.warning(f"⚠️ Fehler beim Stoppen der Master Bot Prozesse: {e}")
            
            # 3. Fallback: pkill für Master Bot
            if not master_bot_stopped:
                try:
                    result = subprocess.run(
                        ['pkill', '-f', 'master_bot.py'],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    logger.info("✅ Master Bot gestoppt (pkill fallback)")
                    master_bot_stopped = True
                except Exception as e:
                    logger.warning(f"⚠️ pkill fehlgeschlagen: {e}")
            
            time.sleep(1)  # Warte kurz, damit alle Prozesse beendet sind
            
            return {"success": True, "message": "Master Bot gestoppt - Alle Bot-Prozesse wurden gestoppt"}
        
        else:
            return {"success": False, "error": f"Unbekannter Service: {service_name}"}
    
    except Exception as e:
        logger.error(f"Error stopping service {service_name}: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def _run_stop_fixed_cycle_script(script_path: Path, project_root: Path) -> dict:
    logger.info(
        "stop_fixed_cycle_script_begin %s",
        {"script_path": str(script_path), "project_root": str(project_root)},
    )
    if not script_path.exists():
        logger.error("stop_fixed_cycle.sh missing at %s", script_path)
        return {"success": False, "error": "stop_fixed_cycle.sh not found"}

    try:
        result = subprocess.run(
            [str(script_path)],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            logger.info(
                "stop_fixed_cycle_script_success %s",
                {"stdout": result.stdout.strip()},
            )
            return {"success": True, "message": "Fixed cycle stop script executed"}
        logger.warning(
            "stop_fixed_cycle_script_failed %s",
            {
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            },
        )
        return {
            "success": False,
            "error": result.stderr or f"Return code {result.returncode}",
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        logger.error("stop_fixed_cycle.sh timed out")
        return {"success": False, "error": "stop_fixed_cycle.sh timed out"}
    except Exception as exc:
        logger.error("stop_fixed_cycle.sh failed: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc)}


@app.post("/api/scripts/stop-fixed-cycle")
@app.post("/dashboard/api/scripts/stop-fixed-cycle")
async def api_run_stop_fixed_cycle_script(user: dict = Depends(require_auth)):
    """Run scripts/stop_fixed_cycle.sh when the dashboard stop button is clicked."""
    logger.info("stop_fixed_cycle_api_requested %s", {"user": user.get("username")})
    project_root = Path(__file__).resolve().parent.parent
    script_path = project_root / "scripts" / "stop_fixed_cycle.sh"
    if not script_path.exists():
        logger.error("stop_fixed_cycle.sh missing at %s", script_path)
        return JSONResponse({"success": False, "error": "stop_fixed_cycle.sh not found"}, status_code=400)
    return _run_stop_fixed_cycle_script(script_path, project_root)


def _run_restart_fixed_cycle_script(script_path: Path, project_root: Path) -> dict:
    logger.info(
        "restart_fixed_cycle_script_begin %s",
        {"script_path": str(script_path), "project_root": str(project_root)},
    )
    if not script_path.exists():
        logger.error("restart_fixed_cycle.sh missing at %s", script_path)
        return {"success": False, "error": "restart_fixed_cycle.sh not found"}

    try:
        result = subprocess.run(
            [str(script_path)],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            logger.info(
                "restart_fixed_cycle_script_success %s",
                {"stdout": result.stdout.strip()},
            )
            return {"success": True, "message": "Fixed cycle restart script executed"}
        logger.warning(
            "restart_fixed_cycle_script_failed %s",
            {
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            },
        )
        return {
            "success": False,
            "error": result.stderr or f"Return code {result.returncode}",
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        logger.error("restart_fixed_cycle.sh timed out")
        return {"success": False, "error": "restart_fixed_cycle.sh timed out"}
    except Exception as exc:
        logger.error("restart_fixed_cycle.sh failed: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc)}


@app.post("/api/scripts/restart-fixed-cycle")
@app.post("/dashboard/api/scripts/restart-fixed-cycle")
async def api_run_restart_fixed_cycle_script(user: dict = Depends(require_auth)):
    """Execute scripts/restart_fixed_cycle.sh when Start button is clicked."""
    logger.info("restart_fixed_cycle_api_requested %s", {"user": user.get("username")})
    project_root = Path(__file__).resolve().parent.parent
    script_path = project_root / "scripts" / "restart_fixed_cycle.sh"
    if not script_path.exists():
        logger.error("restart_fixed_cycle.sh missing at %s", script_path)
        return JSONResponse({"success": False, "error": "restart_fixed_cycle.sh not found"}, status_code=400)
    _maybe_run_dashboard_start_snapshot(project_root=project_root)
    return _run_restart_fixed_cycle_script(script_path, project_root)


def _is_valid_long_bot_name(bot_name: str) -> bool:
    bot, error = _validate_long_bot_request(bot_name)
    return bot is not None and error is None


def _run_long_bot_script(script_path: Path, bot_name: str, script_label: str, project_root: Path) -> dict:
    logger.info(
        "%s_script_begin %s",
        script_label,
        {"script_path": str(script_path), "bot_name": bot_name},
    )
    if not _is_executable_script(script_path):
        logger.error("%s missing at %s", script_label, script_path)
        return {"success": False, "error": f"{script_label} not found"}

    try:
        result = subprocess.run(
            [str(script_path), bot_name],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONPATH": str(project_root)},
        )
        if result.returncode == 0:
            logger.info(
                "%s_script_success %s",
                script_label,
                {"bot_name": bot_name, "stdout": result.stdout.strip()},
            )
            return {
                "success": True,
                "message": f"{script_label} executed for {bot_name}",
                "stdout": result.stdout,
            }
        logger.warning(
            "%s_script_failed %s",
            script_label,
            {
                "bot_name": bot_name,
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            },
        )
        return {
            "success": False,
            "error": result.stderr or f"Return code {result.returncode}",
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        logger.error("%s timed out for %s", script_label, bot_name)
        return {"success": False, "error": f"{script_label} timed out"}
    except Exception as exc:
        logger.error("%s failed for %s: %s", script_label, bot_name, exc, exc_info=True)
        return {"success": False, "error": str(exc)}


def _start_long_bot_script_async(script_path: Path, bot_name: str, script_label: str, project_root: Path) -> dict:
    logger.info(
        "%s_script_begin_async %s",
        script_label,
        {"script_path": str(script_path), "bot_name": bot_name},
    )
    if not _is_executable_script(script_path):
        logger.error("%s missing at %s", script_label, script_path)
        return {"success": False, "error": f"{script_label} not found"}

    launcher_log = (
        project_root
        / "live_bots"
        / "100_50_hedge_bot"
        / bot_name
        / "logs"
        / f"{script_label}_launcher.log"
    )
    launcher_log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(launcher_log, "a", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                [str(script_path), bot_name],
                cwd=str(project_root),
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
                env={**os.environ, "PYTHONPATH": str(project_root)},
            )
        logger.info(
            "%s_script_spawned %s",
            script_label,
            {"bot_name": bot_name, "pid": process.pid, "launcher_log": str(launcher_log)},
        )
        return {
            "success": True,
            "message": f"{script_label} started for {bot_name}",
            "pid": process.pid,
            "launcher_log": str(launcher_log),
        }
    except Exception as exc:
        logger.error("%s spawn failed for %s: %s", script_label, bot_name, exc, exc_info=True)
        return {"success": False, "error": str(exc), "launcher_log": str(launcher_log)}


@app.post("/api/scripts/start-long-bot")
@app.post("/dashboard/api/scripts/start-long-bot")
async def api_run_start_long_bot_script(
    user: dict = Depends(require_auth),
    body: Optional[dict] = Body(None),
):
    bot_name = (body or {}).get("bot_name") if body else None
    bot, error = _validate_long_bot_request(bot_name)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=400)
    project_root = Path(__file__).resolve().parent.parent
    _maybe_run_dashboard_start_snapshot(bot_name=bot_name, project_root=project_root)
    script_path = _long_bot_shared_script_path("start")
    if not _is_executable_script(script_path):
        return JSONResponse({"success": False, "error": f"Central start script missing: {script_path}"}, status_code=400)
    return _start_long_bot_script_async(script_path, bot_name, "start_long_bot", project_root)


@app.post("/api/scripts/stop-long-bot")
@app.post("/dashboard/api/scripts/stop-long-bot")
async def api_run_stop_long_bot_script(
    user: dict = Depends(require_auth),
    body: Optional[dict] = Body(None),
):
    bot_name = (body or {}).get("bot_name") if body else None
    bot, error = _validate_long_bot_request(bot_name)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=400)
    project_root = Path(__file__).resolve().parent.parent
    script_path = _long_bot_shared_script_path("stop")
    if not _is_executable_script(script_path):
        return JSONResponse({"success": False, "error": f"Central stop script missing: {script_path}"}, status_code=400)
    return _run_long_bot_script(script_path, bot_name, "stop_long_bot", project_root)


@app.post("/api/scripts/restart-long-bot")
@app.post("/dashboard/api/scripts/restart-long-bot")
async def api_run_restart_long_bot_script(
    user: dict = Depends(require_auth),
    body: Optional[dict] = Body(None),
):
    bot_name = (body or {}).get("bot_name") if body else None
    bot, error = _validate_long_bot_request(bot_name)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=400)
    project_root = Path(__file__).resolve().parent.parent
    _maybe_run_dashboard_start_snapshot(bot_name=bot_name, project_root=project_root)
    stop_script = _long_bot_shared_script_path("stop")
    start_script = _long_bot_shared_script_path("start")
    if not _is_executable_script(stop_script):
        return JSONResponse({"success": False, "error": f"Central stop script missing: {stop_script}"}, status_code=400)
    if not _is_executable_script(start_script):
        return JSONResponse({"success": False, "error": f"Central start script missing: {start_script}"}, status_code=400)
    stop_result = _run_long_bot_script(stop_script, bot_name, "restart_long_bot_stop", project_root)
    if not stop_result.get("success"):
        return stop_result
    time.sleep(1)
    return _start_long_bot_script_async(start_script, bot_name, "restart_long_bot_start", project_root)


@app.post("/api/scripts/stop-long-bot-with-cleanup")
@app.post("/dashboard/api/scripts/stop-long-bot-with-cleanup")
async def api_run_stop_long_bot_cleanup_script(
    user: dict = Depends(require_auth),
    body: Optional[dict] = Body(None),
):
    bot_name = (body or {}).get("bot_name") if body else None
    bot, error = _validate_long_bot_request(bot_name)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=400)
    project_root = Path(__file__).resolve().parent.parent
    script_path = _long_bot_shared_script_path("stop_with_cleanup")
    if not _is_executable_script(script_path):
        return JSONResponse({"success": False, "error": f"Central cleanup script missing: {script_path}"}, status_code=400)
    return _run_long_bot_script(script_path, bot_name, "stop_with_cleanup", project_root)


if __name__ == "__main__":
    # Bind to 0.0.0.0 to allow access from other devices
    # WICHTIG: reload=False für Production (reload=True kann zu Instabilität führen)
    import os
    import subprocess
    reload_enabled = os.getenv("DASHBOARD_RELOAD", "false").lower() == "true"
    
    logger.info("=" * 80)
    logger.info("🚀 Dashboard wird gestartet...")
    logger.info(f"📁 Working Directory: {os.getcwd()}")
    logger.info(f"🌐 Host: 0.0.0.0, Port: 3000")
    logger.info(f"🔄 Reload: {reload_enabled}")
    logger.info("=" * 80)
    
    logger.info("🐕 Watchdog-Autostart deaktiviert, um Fremd-Dashboard-Reaktivierung zu vermeiden")
    
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=3000,
        reload=reload_enabled,
        log_level="info",
        access_log=True,
        timeout_keep_alive=75,  # Erhöhte Timeout-Werte für Stabilität
        timeout_graceful_shutdown=30
    )
