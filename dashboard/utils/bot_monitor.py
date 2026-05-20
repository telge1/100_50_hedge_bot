"""
Bot monitoring utilities
"""
import logging
import subprocess
import json
import os
import re
from pathlib import Path
from typing import Optional, Tuple, Any
from .config_manager import load_config
from .bot_profiles import is_bot_profile
try:
    from dashboard.bot_registry import get_bot_profiles, get_bot_paths
except ImportError:
    from bot_registry import get_bot_profiles, get_bot_paths

logger = logging.getLogger(__name__)

# Try to import psutil for process checking
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


def _get_project_root() -> Path:
    """Project root: from env (set by dashboard app) or from this file's path."""
    env_root = os.environ.get("BURN_REENTRY_PROJECT_ROOT", "").strip()
    if env_root and Path(env_root).is_dir():
        return Path(env_root).resolve()
    # dashboard/utils/bot_monitor.py -> parent.parent.parent = project root
    return Path(__file__).resolve().parent.parent.parent


def _is_matching_bot_process(symbol: str, bot_type: str, profile: str = None) -> bool:
    """Best-effort process scan fallback for script-started bots.
    profile: main|None = nur Main-Bot (kein bot_N); bot_N = profil-spezifisch.
    Der Prozess-Scan kann bot_N nicht zuverlässig unterscheiden – daher bei main
    Prozesse mit 'bot_<n>' im Cmdline ausschließen."""
    if not PSUTIL_AVAILABLE:
        return False
    try:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return False
        script_name = f"{bot_type}_bot.py"
        prof = (profile or "").strip()
        exclude_bot_profiles = not is_bot_profile(prof)  # Bei main: nur Main-Bot zählen
        for proc in psutil.process_iter(["cmdline", "status"]):
            try:
                if proc.info.get("status") in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                    continue
                cmdline = proc.info.get("cmdline") or []
                if not cmdline:
                    continue
                cmd_str = " ".join(str(p) for p in cmdline).upper()
                cmd_upper = [str(part).upper() for part in cmdline]
                has_script = any(part.endswith(script_name.upper()) for part in cmd_upper)
                has_symbol = normalized_symbol in cmd_upper
                if has_script and has_symbol:
                    if exclude_bot_profiles and re.search(r"BOT_[0-9]+", cmd_str):
                        continue  # Das ist bot_N – für Main-Profil nicht zählen
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue
    except Exception:
        return False
    return False


def is_bot_running(symbol: str, bot_type: str = "long", profile: str = None) -> bool:
    """Check if bot is running via systemctl or PID file (local mode).
    profile: bot_N für profil-spezifische PID-Dateien (data/run/long_bot_SYMBOL_bot_N.pid)."""
    import time
    import logging
    logger = logging.getLogger(__name__)
    prof = (profile or "").strip()
    service_name = f'hedgebot-{bot_type}@{symbol}'
    project_dir = _get_project_root()

    # For registry-backed bot_N long bots, the canonical runtime PID lives in
    # live_bots/.../long_bot_N/run/bot.pid. Check this before legacy PID flows.
    runtime_pid, runtime_pid_path = _get_registry_profile_runtime_pid(prof, bot_type)
    if runtime_pid:
        logger.debug("Registry runtime PID marks bot as running: %s (%s)", runtime_pid, runtime_pid_path)
        return True

    pid_file = project_dir / "logs" / "fixed_cycle_bot.pid"
    if not is_bot_profile(prof) and pid_file.exists():
        pid_valid = False
        pid_cmdline = None
        pid_value = None
        try:
            pid_raw = pid_file.read_text(encoding="utf-8").strip()
            pid_value = int(pid_raw) if pid_raw.isdigit() else None
        except Exception as exc:
            logger.debug("Invalid PID file %s: %s", pid_file, exc)
        if pid_value:
            try:
                if PSUTIL_AVAILABLE:
                    proc = psutil.Process(pid_value)
                    if proc.is_running():
                        cmdline = proc.cmdline() or []
                        pid_cmdline = " ".join(str(x) for x in cmdline)
                        if any("fixed_cycle_hedge_bot.runner" in str(part) for part in cmdline):
                            pid_valid = True
                else:
                    os.kill(pid_value, 0)
                    pid_valid = True
            except Exception as exc:
                logger.debug("PID %s invalid: %s", pid_value, exc)
        logger.debug(
            "PID-file check: path=%s pid=%s valid=%s cmdline=%s",
            pid_file,
            pid_value,
            pid_valid,
            pid_cmdline,
        )
        if not pid_valid:
            try:
                pid_file.unlink(missing_ok=True)
                logger.debug("Removed stale PID file %s", pid_file)
            except Exception:
                pass
        else:
            logger.debug("PID-file marks fixed-cycle bot as running (pid=%s)", pid_value)
            return True

    # First try systemd service (with one retry after 1s to avoid false "stopped" after dashboard restart)
    if not is_bot_profile(prof):
        for attempt in range(2):
            try:
                result = subprocess.run(
                    ['systemctl', 'is-active', service_name],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                status = (result.stdout or "").strip()
                if status == 'active':
                    return True
                if attempt == 0 and status != 'active':
                    time.sleep(0.3)
                    continue
                break
            except Exception as e:
                logger.debug(f"Error checking systemctl status for {service_name} (attempt {attempt + 1}): {e}")
                if attempt == 0:
                    time.sleep(0.3)
                    continue
                break

    # Fallback: Check local mode via PID file (nur für main – local_bots_pids.json hat kein Profil)
    if not is_bot_profile(prof):
        try:
            project_dir = _get_project_root()
            pid_file = project_dir / "data" / "logs" / "local_bots_pids.json"
            if pid_file.exists():
                with open(pid_file, 'r') as f:
                    pids_dict = json.load(f)
                bot_key = f"{bot_type}_{symbol}"
                if bot_key in pids_dict:
                    pid = pids_dict[bot_key]
                    if PSUTIL_AVAILABLE:
                        try:
                            process = psutil.Process(pid)
                            if not process.is_running():
                                return False
                            try:
                                status = process.status()
                                if status in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                                    return False
                            except Exception:
                                pass
                            return True
                        except psutil.NoSuchProcess:
                            return False
                        except psutil.AccessDenied:
                            try:
                                os.kill(pid, 0)
                                return True
                            except (OSError, ProcessLookupError):
                                return False
                    else:
                        try:
                            os.kill(pid, 0)
                            return True
                        except (OSError, ProcessLookupError):
                            return False
        except Exception:
            pass

    # Legacy fallback for older/main script-started bots: PID files in data/run.
    # For bot_N long bots, the canonical location is live_bots/.../run/bot.pid.
    try:
        project_dir = _get_project_root()
        run_dir = project_dir / "data" / "run"
        safe_symbol = ''.join(ch if (ch.isalnum() or ch in "_-") else '_' for ch in str(symbol or "").strip().upper())
        prof_suffix = f"_{profile}" if is_bot_profile((profile or "").strip()) else ""
        pid_name = f"{bot_type}_bot_{safe_symbol}{prof_suffix}.pid"
        pid_file = run_dir / pid_name
        if pid_file.exists():
            pid_raw = pid_file.read_text(encoding="utf-8").strip()
            if pid_raw.isdigit():
                pid = int(pid_raw)
                running = False
                def _pid_alive() -> bool:
                    if not PSUTIL_AVAILABLE:
                        try:
                            os.kill(pid, 0)
                            return True
                        except (OSError, ProcessLookupError):
                            return False
                    try:
                        process = psutil.Process(pid)
                        if not process.is_running():
                            return False
                        try:
                            status = process.status()
                            if status in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                                return False
                        except Exception:
                            pass
                        return True
                    except psutil.NoSuchProcess:
                        return False
                    except psutil.AccessDenied:
                        try:
                            os.kill(pid, 0)
                            return True
                        except (OSError, ProcessLookupError):
                            return False

                if _pid_alive():
                    return True
                try:
                    pid_file.unlink(missing_ok=True)
                except Exception:
                    pass
                return False
    except Exception:
        pass

    # Final fallback: scan running processes (profil-aware – bei main: bot_N ausschließen)
    if not is_bot_profile(prof):
        try:
            if _is_matching_bot_process(symbol, bot_type, profile=prof or None):
                return True
        except Exception:
            pass

    return False


def _read_bot_status_from_run(bot_name: str) -> dict[str, Any]:
    project_root = _get_project_root()
    bot_dir = project_root / "live_bots" / "100_50_hedge_bot" / bot_name
    pid_path = bot_dir / "run" / "bot.pid"
    status_path = bot_dir / "run" / "status.json"
    status_payload = {}

    if status_path.exists():
        try:
            status_payload = json.loads(status_path.read_text(encoding="utf-8") or "{}")
        except Exception:
            status_payload = {}

    return {
        "bot_dir": bot_dir,
        "pid_path": pid_path,
        "status_payload": status_payload,
    }


def _pid_runs_same_bot(pid: int, bot_name: str) -> bool:
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if not cmdline_path.exists():
        return False
    try:
        cmdline = cmdline_path.read_bytes().decode("utf-8").replace("\x00", " ")
    except Exception:
        return False
    return (
        "fixed_cycle_hedge_bot.runner" in cmdline and f"--bot-name {bot_name}" in cmdline
    )


def _resolve_profile_bot_record(profile: str | None) -> dict[str, Any] | None:
    prof = (profile or "").strip().lower()
    if not is_bot_profile(prof):
        return None
    for bot in get_bot_profiles():
        if str(bot.get("profile") or "").strip().lower() == prof:
            return bot
    return None


def _resolve_profile_long_bot_name(profile: str | None) -> str | None:
    bot = _resolve_profile_bot_record(profile)
    if not bot:
        return None
    return str(bot.get("bot_name") or "").strip() or None


def _read_pid_cmdline(pid: int) -> str:
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if cmdline_path.exists():
        try:
            return cmdline_path.read_bytes().decode("utf-8").replace("\x00", " ")
        except Exception:
            return ""
    if PSUTIL_AVAILABLE:
        try:
            process = psutil.Process(pid)
            return " ".join(str(part) for part in (process.cmdline() or []))
        except Exception:
            return ""
    return ""


def _registry_profile_runtime_info(profile: str | None, bot_type: str) -> dict[str, Any] | None:
    if (bot_type or "").strip().lower() != "long":
        return None
    bot = _resolve_profile_bot_record(profile)
    if not bot:
        return None
    bot_name = str(bot.get("bot_name") or "").strip()
    if not bot_name:
        return None
    paths = get_bot_paths(bot_name)
    if not paths:
        return None
    bot_dir = paths["bot_dir"]
    return {
        "bot_name": bot_name,
        "pid_path": bot_dir / "run" / "bot.pid",
        "config_path": bot_dir / "config" / "fixed_cycle_config.json",
        "state_path": paths["state_file"],
    }


def _is_valid_registry_runtime_pid(
    pid: int,
    *,
    bot_name: str,
    config_path: Path,
    state_path: Path,
) -> bool:
    if not _pid_alive(pid):
        return False
    cmdline = _read_pid_cmdline(pid)
    if not cmdline:
        return True
    if "fixed_cycle_hedge_bot.runner" not in cmdline:
        return False
    return any(
        token in cmdline
        for token in (
            f"--bot-name {bot_name}",
            str(config_path),
            str(state_path),
        )
    )


def _get_registry_profile_runtime_pid(profile: str | None, bot_type: str) -> Tuple[Optional[int], Optional[Path]]:
    runtime_info = _registry_profile_runtime_info(profile, bot_type)
    if not runtime_info:
        return (None, None)
    pid_path = runtime_info["pid_path"]
    if not pid_path.exists():
        return (None, None)
    try:
        pid_raw = pid_path.read_text(encoding="utf-8").strip()
        if not pid_raw.isdigit():
            pid_path.unlink(missing_ok=True)
            return (None, None)
        pid = int(pid_raw)
        if _is_valid_registry_runtime_pid(
            pid,
            bot_name=runtime_info["bot_name"],
            config_path=runtime_info["config_path"],
            state_path=runtime_info["state_path"],
        ):
            return (pid, pid_path)
        pid_path.unlink(missing_ok=True)
    except Exception:
        pass
    return (None, None)


def get_bot_status(
    symbol: str,
    bot_type: str = "long",
    bot_name: str | None = None,
    profile: str | None = None,
) -> dict:
    prof = (profile or "").strip()
    if bot_name is None and bot_type == "long":
        bot_name = _resolve_profile_long_bot_name(prof)

    payload = {}
    pid_value = None
    pid_path = None

    if bot_name:
        bot_info = _read_bot_status_from_run(bot_name)
        pid_path = bot_info["pid_path"]
        payload = bot_info["status_payload"]

    payload_status = payload.get("status")
    payload_symbol = payload.get("symbol")

    logger.debug("Bot %s status payload=%s", bot_name, payload_status)

    running = False
    valid_pid = False

    if pid_path and pid_path.exists():
        try:
            pid_raw = pid_path.read_text(encoding="utf-8").strip()
            pid_value = int(pid_raw) if pid_raw.isdigit() else None
        except Exception:
            pid_value = None
        if pid_value and _pid_alive(pid_value):
            cmdline = _read_pid_cmdline(pid_value)
            if (
                "fixed_cycle_hedge_bot.runner" in cmdline
                and bot_name
                and f"--bot-name {bot_name}" in cmdline
            ):
                running = True
                valid_pid = True
                logger.debug("Valid PID %s for %s", pid_value, bot_name)
            else:
                logger.debug("PID %s cmdline mismatch for %s: %s", pid_value, bot_name, cmdline)
        else:
            logger.debug("PID %s not alive for %s", pid_value, bot_name)
        if not valid_pid and pid_path.exists():
            try:
                pid_path.unlink(missing_ok=True)
                logger.debug("Removed stale PID file %s", pid_path)
            except Exception:
                pass

    # For diagnostics only; must not flip running flag if valid_pid=false
    fallback_running = is_bot_running(symbol, bot_type=bot_type, profile=prof or None)
    logger.debug("Fallback is_bot_running(%s) => %s", bot_name, fallback_running)

    if payload_status == "stopped" and not valid_pid:
        logger.debug("Payload status stopped overrides running flag for %s", bot_name)
        running = False
    elif valid_pid:
        running = True
    else:
        running = False

    if running and valid_pid:
        return {
            "bot_name": bot_name,
            "bot_type": bot_type,
            "symbol": payload_symbol or symbol,
            "running": True,
            "stale_pid": False,
            "start_requested": True,
            "status": "running",
            "status_label": f"läuft{'' if not payload_symbol else ': ' + payload_symbol}",
            "pid": pid_value,
            "status_source": "validated_run_pid",
        }

    if payload_status == "waiting_for_symbol" and bool(payload.get("start_requested")):
        return {
            "bot_name": bot_name,
            "bot_type": bot_type,
            "symbol": payload_symbol,
            "running": False,
            "status": "waiting_for_symbol",
            "status_label": payload_symbol
            and payload.get("reserved_by")
            and f"{payload_symbol} reserviert von {payload.get('reserved_by')}"
            or "wartet auf neuen Coin",
            "reserved_by": payload.get("reserved_by"),
            "reason": payload.get("reason"),
            "pid": None,
            "start_requested": True,
            "status_source": "run_status_json_waiting",
        }

    return {
        "bot_name": bot_name,
        "bot_type": bot_type,
        "symbol": payload_symbol or symbol,
        "running": False,
        "start_requested": False,
        "status": "stopped",
        "status_label": "gestoppt",
        "pid": None,
        "status_source": "no_valid_runner_pid",
    }


def get_fixed_cycle_symbol() -> Optional[str]:
    """Symbol configured in fixed_cycle_config.json, if present."""
    try:
        project_dir = _get_project_root()
        config_file = project_dir / "fixed_cycle_hedge_bot" / "config" / "fixed_cycle_config.json"
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            symbol = str(data.get("symbol") or "").strip().upper()
            return symbol if symbol else None
    except Exception:
        pass
    return None


def find_fixed_cycle_runner_pid() -> Optional[int]:
    """Find PID of manually started fixed_cycle_hedge_bot.runner process."""
    pattern = "fixed_cycle_hedge_bot.runner"
    if PSUTIL_AVAILABLE:
        for proc in psutil.process_iter(["pid", "cmdline", "status"]):
            try:
                if proc.info.get("status") in (psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE):
                    continue
                cmdline = proc.info.get("cmdline") or []
                if any(pattern in str(part) for part in cmdline):
                    return proc.info.get("pid")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    else:
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                pid_str = (result.stdout or "").strip().splitlines()[0]
                if pid_str.isdigit():
                    return int(pid_str)
        except Exception:
            pass
    return None


def get_bot_pid_from_run_dir(symbol: str, bot_type: str, profile: str | None = None) -> Tuple[Optional[int], Optional[Path]]:
    """
    Find a running bot via canonical runtime PID first, then legacy data/run fallback.
    Checks for bot_N long bots: live_bots/.../long_bot_N/run/bot.pid.
    Checks otherwise: {bot_type}_bot_{symbol}.pid and {bot_type}_bot_{symbol}_bot_<n>.pid.
    Returns (pid, pid_file) if found and process alive, else (None, None).
    """
    runtime_pid, runtime_pid_path = _get_registry_profile_runtime_pid(profile, bot_type)
    if runtime_pid:
        return (runtime_pid, runtime_pid_path)

    project_dir = _get_project_root()
    run_dir = project_dir / "data" / "run"
    safe_symbol = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in str(symbol or "").strip().upper())
    prof = (profile or "").strip().lower()
    if is_bot_profile(prof):
        suffixes = [f"_{prof}"]
    else:
        suffixes = [""]
        try:
            suffixes.extend(
                sorted(
                    {
                        pid_path.stem[len(f"{bot_type}_bot_{safe_symbol}"):]
                        for pid_path in run_dir.glob(f"{bot_type}_bot_{safe_symbol}_bot_*.pid")
                        if pid_path.stem.startswith(f"{bot_type}_bot_{safe_symbol}_bot_")
                    }
                )
            )
        except Exception:
            pass
    for suf in suffixes:
        pid_name = f"{bot_type}_bot_{safe_symbol}{suf}.pid"
        pid_file = run_dir / pid_name
        if pid_file.exists():
            try:
                pid_raw = pid_file.read_text(encoding="utf-8").strip()
                if pid_raw.isdigit():
                    pid = int(pid_raw)
                    if _pid_alive(pid):
                        return (pid, pid_file)
            except Exception:
                pass
    return (None, None)


def _pid_alive(pid: int) -> bool:
    """Check if process with given PID is running (not zombie/dead)."""
    if not pid or pid <= 0:
        return False
    if PSUTIL_AVAILABLE:
        try:
            process = psutil.Process(pid)
            if not process.is_running():
                return False
            try:
                if process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                    return False
            except Exception:
                pass
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def get_symbols_with_running_bots() -> list:
    """
    Return only symbols that have at least one bot currently running.
    Uses only strict sources: running systemd units and alive PID files in data/run.
    Does NOT include symbols from inactive systemd, log files, or process scan
    (so closed trades / stopped bots are not shown in the dropdown).
    """
    symbols = set()
    project_dir = _get_project_root()

    # 1) Only running systemd services (no inactive)
    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=running", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                for prefix in ("hedgebot-long@", "hedgebot-short@"):
                    if prefix in line:
                        parts = line.split()
                        if parts:
                            name = parts[0]
                            if "@" in name:
                                symbol = name.split("@")[1].split(".")[0]
                                symbols.add(symbol)
                        break
    except Exception:
        pass

    # 2) data/run/*.pid only if process is actually alive (stale files ignored)
    try:
        run_dir = project_dir / "data" / "run"
        if run_dir.exists():
            for pid_path in run_dir.glob("*_bot_*.pid"):
                stem = pid_path.stem
                if stem.startswith("long_bot_"):
                    symbol = stem[len("long_bot_"):]
                elif stem.startswith("short_bot_"):
                    symbol = stem[len("short_bot_"):]
                else:
                    continue
                try:
                    raw = pid_path.read_text(encoding="utf-8").strip()
                    if raw.isdigit() and _pid_alive(int(raw)):
                        symbols.add(symbol)
                except Exception:
                    try:
                        pid_path.unlink(missing_ok=True)
                    except Exception:
                        pass
    except Exception:
        pass

    return sorted(symbols)


def get_all_bots(bot_type: str = None, include_inactive: bool = True) -> list:
    """
    Get all bot instances (from systemd services, including inactive ones)
    
    Args:
        bot_type: "long", "short", or None (for both)
        include_inactive: If True, also check inactive services and PID file
    
    Returns:
        List of bot status dicts with bot_type field
    """
    bots = []
    found_symbols = set()
    found_bot_names = set()

    # Registry-backed long_bot_X cards must always come from get_bot_profiles() +
    # strict get_bot_status(... bot_name=..., profile=...).
    if bot_type is None or bot_type == "long":
        for entry in get_bot_profiles():
            bot_name = str(entry.get("bot_name") or "").strip()
            profile = str(entry.get("profile") or "").strip()
            if not bot_name:
                continue
            if bot_name in found_bot_names:
                continue
            run_info = _read_bot_status_from_run(bot_name)
            run_payload = run_info.get("status_payload") or {}
            resolved_symbol = str(run_payload.get("symbol") or "").strip().upper()

            status_info = get_bot_status(
                symbol=resolved_symbol,
                bot_type="long",
                bot_name=bot_name,
                profile=profile,
            )

            registry_data = {
                "bot_name": bot_name,
                "profile": profile,
                "label": entry.get("label"),
                "index": entry.get("index"),
                "long_account": entry.get("long_account"),
                "short_account": entry.get("short_account"),
                "bot_dir": str(entry.get("bot_dir")) if entry.get("bot_dir") is not None else None,
            }
            bot_paths = get_bot_paths(bot_name) or {}
            if bot_paths:
                registry_data.update(
                    {
                        "logs_dir": str(bot_paths.get("logs_dir")) if bot_paths.get("logs_dir") else None,
                        "state_file": str(bot_paths.get("state_file")) if bot_paths.get("state_file") else None,
                        "snapshot_file": str(bot_paths.get("snapshot_file")) if bot_paths.get("snapshot_file") else None,
                    }
                )

            card = {}
            card.update(registry_data)
            card.update(status_info)
            bots.append(card)
            found_bot_names.add(bot_name)
            logger.debug(
                "Registry bot card resolved: bot_name=%s running=%s status=%s pid=%s source=%s",
                bot_name,
                card.get("running"),
                card.get("status"),
                card.get("pid"),
                card.get("status_source"),
            )
    
    # Get all systemd services (running AND inactive)
    try:
        # First, list all active services
        result = subprocess.run(
            ['systemctl', 'list-units', '--type=service', '--state=running', '--no-pager'],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            lines = result.stdout.split('\n')
            for line in lines:
                # Look for hedgebot-long@SYMBOL or hedgebot-short@SYMBOL
                # Immer get_bot_status nutzen (ruft is_bot_running auf) – verifiziert, ob Prozess
                # tatsächlich lebt (PID-Check). Verhindert falsch-grüne Anzeige nach manuellem Kill.
                if 'hedgebot-long@' in line:
                    parts = line.split()
                    if parts:
                        service_name = parts[0]
                        if '@' in service_name:
                            symbol = service_name.split('@')[1].split('.')[0]
                            if (bot_type is None or bot_type == "long"):
                                logger.debug("Skipping legacy/systemd long service card for symbol=%s", symbol)
                                found_symbols.add(('long', symbol))
                                continue
                
                elif 'hedgebot-short@' in line:
                    parts = line.split()
                    if parts:
                        service_name = parts[0]
                        if '@' in service_name:
                            symbol = service_name.split('@')[1].split('.')[0]
                            found_symbols.add(('short', symbol))
                            if (bot_type is None or bot_type == "short"):
                                bots.append(get_bot_status(symbol, bot_type="short"))
        
        # Also check inactive services if include_inactive is True
        if include_inactive:
            result = subprocess.run(
                ['systemctl', 'list-units', '--type=service', '--all', '--no-pager'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                lines = result.stdout.split('\n')
                for line in lines:
                    if 'hedgebot-long@' in line or 'hedgebot-short@' in line:
                        parts = line.split()
                        if parts:
                            service_name = parts[0]
                            if '@' in service_name:
                                if 'hedgebot-long@' in service_name:
                                    symbol = service_name.split('@')[1].split('.')[0]
                                    if ('long', symbol) not in found_symbols:
                                        if (bot_type is None or bot_type == "long"):
                                            logger.debug("Skipping inactive legacy/systemd long service card for symbol=%s", symbol)
                                            found_symbols.add(('long', symbol))
                                            continue
                                elif 'hedgebot-short@' in service_name:
                                    symbol = service_name.split('@')[1].split('.')[0]
                                    if ('short', symbol) not in found_symbols:
                                        found_symbols.add(('short', symbol))
                                        if (bot_type is None or bot_type == "short"):
                                            bots.append(get_bot_status(symbol, bot_type="short"))
    except Exception as e:
        pass
    
    # Also check local mode PID file for bots
    if include_inactive:
        try:
            project_dir = _get_project_root()
            pid_file = project_dir / "data" / "logs" / "local_bots_pids.json"
            if pid_file.exists():
                with open(pid_file, 'r') as f:
                    pids_dict = json.load(f)
                
                for bot_key, pid in pids_dict.items():
                    # bot_key format: "long_SYMBOL" or "short_SYMBOL"
                    parts = bot_key.split('_', 1)
                    if len(parts) == 2:
                        bot_type_key, symbol = parts
                        if (bot_type is None or bot_type == bot_type_key):
                            if (bot_type_key, symbol) not in found_symbols:
                                if bot_type_key == "long":
                                    logger.debug("Skipping local_bots_pids long legacy card for symbol=%s", symbol)
                                    found_symbols.add((bot_type_key, symbol))
                                    continue
                                found_symbols.add((bot_type_key, symbol))
                                bots.append(get_bot_status(symbol, bot_type=bot_type_key))
        except Exception as e:
            pass

    # Also check script-based PID files (data/run/*.pid)
    if include_inactive:
        try:
            project_dir = _get_project_root()
            run_dir = project_dir / "data" / "run"
            if run_dir.exists():
                for pid_path in run_dir.glob("*_bot_*.pid"):
                    stem = pid_path.stem  # e.g. long_bot_BTCUSDT
                    if stem.startswith("long_bot_"):
                        bot_type_key = "long"
                        symbol = stem[len("long_bot_"):]
                    elif stem.startswith("short_bot_"):
                        bot_type_key = "short"
                        symbol = stem[len("short_bot_"):]
                    else:
                        continue

                    if (bot_type is None or bot_type == bot_type_key):
                        if (bot_type_key, symbol) not in found_symbols:
                            if bot_type_key == "long":
                                logger.debug("Skipping data/run long legacy card for symbol=%s", symbol)
                                found_symbols.add((bot_type_key, symbol))
                                continue
                            found_symbols.add((bot_type_key, symbol))
                            bots.append(get_bot_status(symbol, bot_type=bot_type_key))
        except Exception:
            pass

    # Final fallback: include running script processes even without systemd/PID artifacts
    if include_inactive and PSUTIL_AVAILABLE:
        try:
            for proc in psutil.process_iter(["cmdline", "status"]):
                try:
                    if proc.info.get("status") in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                        continue
                    cmdline = proc.info.get("cmdline") or []
                    if not cmdline:
                        continue
                    upper_cmd = [str(part).upper() for part in cmdline]
                    bot_type_key = None
                    if any(part.endswith("LONG_BOT.PY") for part in upper_cmd):
                        bot_type_key = "long"
                    elif any(part.endswith("SHORT_BOT.PY") for part in upper_cmd):
                        bot_type_key = "short"
                    if not bot_type_key:
                        continue
                    if bot_type is not None and bot_type != bot_type_key:
                        continue
                    symbol = None
                    for part in cmdline:
                        token = str(part).strip().upper()
                        if token.endswith("USDT"):
                            symbol = token
                            break
                    if not symbol:
                        continue
                    if (bot_type_key, symbol) in found_symbols:
                        continue
                    if bot_type_key == "long":
                        logger.debug("Skipping psutil long legacy card for symbol=%s", symbol)
                        found_symbols.add((bot_type_key, symbol))
                        continue
                    found_symbols.add((bot_type_key, symbol))
                    bots.append(get_bot_status(symbol, bot_type=bot_type_key))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                except Exception:
                    continue
        except Exception:
            pass
    
    return bots


def check_service_status(service_name: str) -> dict:
    """Check if a systemd service is active"""
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', service_name],
            capture_output=True,
            text=True,
            timeout=5
        )
        is_active = result.stdout.strip() == 'active'
        
        # Get more detailed status
        enabled_result = subprocess.run(
            ['systemctl', 'is-enabled', service_name],
            capture_output=True,
            text=True,
            timeout=5
        )
        is_enabled = enabled_result.stdout.strip() in ['enabled', 'enabled-runtime']
        
        return {
            "active": is_active,
            "enabled": is_enabled,
            "status": "active" if is_active else "inactive"
        }
    except Exception as e:
        return {
            "active": False,
            "enabled": False,
            "status": "error",
            "error": str(e)
        }


def is_master_bot_running() -> bool:
    """Check if master bot is running (systemd or process)"""
    # Try systemd service first
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', 'hedgebot-master.service'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.stdout.strip() == 'active':
            return True
    except:
        pass
    
    # Fallback: Check for Python process
    try:
        if PSUTIL_AVAILABLE:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = proc.info.get('cmdline', [])
                    if cmdline:
                        # Prüfe alle cmdline-Argumente als String (für bash-Wrapper)
                        cmdline_str = ' '.join(str(arg) for arg in cmdline)
                        if 'master_bot.py' in cmdline_str:
                            return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        else:
            # Fallback: pgrep
            result = subprocess.run(
                ['pgrep', '-f', 'master_bot.py'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return True
    except Exception as e:
        # Debug: Log error if needed
        pass
    
    return False


def is_any_bot_running(bot_type: str) -> bool:
    """Prüft, ob irgendein Long- oder Short-Bot (beliebiges Symbol) läuft (PID in data/run)."""
    if bot_type not in ("long", "short"):
        return False
    try:
        project_dir = _get_project_root()
        run_dir = project_dir / "data" / "run"
        if not run_dir.exists():
            return False
        prefix = f"{bot_type}_bot_"
        for pid_path in run_dir.glob(f"{prefix}*.pid"):
            try:
                pid_raw = pid_path.read_text(encoding="utf-8").strip()
                if not pid_raw.isdigit():
                    continue
                pid = int(pid_raw)
                if PSUTIL_AVAILABLE:
                    try:
                        p = psutil.Process(pid)
                        if p.is_running():
                            return True
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                else:
                    try:
                        os.kill(pid, 0)
                        return True
                    except (OSError, ProcessLookupError):
                        continue
            except Exception:
                continue
    except Exception:
        pass
    return False


def _hedge_guardian_scope_running(scope: str) -> bool:
    """Prüft, ob der Hedge-Guardian für den angegebenen Scope (main|sub) läuft."""
    pattern = f"hedge_guardian.py --account-scope {scope}"
    try:
        if PSUTIL_AVAILABLE:
            for proc in psutil.process_iter(['pid', 'cmdline']):
                try:
                    cmdline = proc.info.get('cmdline') or []
                    cmdline_str = ' '.join(str(arg) for arg in cmdline)
                    if 'hedge_guardian.py' in cmdline_str and f'--account-scope {scope}' in cmdline_str:
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        result = subprocess.run(
            ['pgrep', '-f', pattern],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def get_hedge_guardian_scope_status(scope: str) -> dict:
    """Status für einen Guardian-Scope (main|sub): active + symbol aus data/state/hedge_guardian_<scope>.json."""
    active = _hedge_guardian_scope_running(scope)
    symbol = None
    try:
        root = _get_project_root()
        path = root / "data" / "state" / f"hedge_guardian_{scope}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            symbol = (data.get("symbol") or "").strip() or None
    except Exception:
        pass
    return {"active": active, "symbol": symbol}


def is_hedge_guardian_running() -> bool:
    """Prüft, ob mindestens ein Hedge-Guardian-Prozess läuft (hedge_guardian.py)."""
    return _hedge_guardian_scope_running("main") or _hedge_guardian_scope_running("sub")


def find_all_master_bot_processes() -> list:
    """Findet alle Master Bot Prozesse (master_bot.py, nicht master_bot_api.py) und gibt deren PIDs zurück"""
    pids = []
    try:
        if PSUTIL_AVAILABLE:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = proc.info.get('cmdline', [])
                    if cmdline:
                        cmdline_str = ' '.join(str(arg) for arg in cmdline)
                        if 'master_bot.py' in cmdline_str and 'master_bot_api.py' not in cmdline_str:
                            pids.append(proc.info['pid'])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        else:
            # Fallback: pgrep
            result = subprocess.run(
                ['pgrep', '-f', 'master_bot.py'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                # Filtere master_bot_api.py heraus
                for pid_str in result.stdout.strip().split('\n'):
                    if pid_str:
                        try:
                            pid = int(pid_str)
                            # Prüfe cmdline für diesen PID
                            cmd_result = subprocess.run(
                                ['ps', '-p', str(pid), '-o', 'cmd='],
                                capture_output=True,
                                text=True,
                                timeout=2
                            )
                            if cmd_result.returncode == 0:
                                cmdline = cmd_result.stdout.strip()
                                if 'master_bot.py' in cmdline and 'master_bot_api.py' not in cmdline:
                                    pids.append(pid)
                        except (ValueError, Exception):
                            continue
    except Exception as e:
        pass
    return pids


def find_all_master_bot_api_processes() -> list:
    """Findet alle Master Bot API Prozesse (master_bot_api.py) und gibt deren PIDs zurück"""
    pids = []
    try:
        if PSUTIL_AVAILABLE:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = proc.info.get('cmdline', [])
                    if cmdline:
                        cmdline_str = ' '.join(str(arg) for arg in cmdline)
                        if 'master_bot_api.py' in cmdline_str:
                            pids.append(proc.info['pid'])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        else:
            # Fallback: pgrep
            result = subprocess.run(
                ['pgrep', '-f', 'master_bot_api.py'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                pids = [int(pid) for pid in result.stdout.strip().split('\n') if pid]
    except Exception as e:
        pass
    return pids


def is_master_bot_api_running() -> bool:
    """Prüft, ob die Master Bot API läuft"""
    return len(find_all_master_bot_api_processes()) > 0


def is_dashboard_running() -> bool:
    """Check if dashboard is running (process, not systemd service)"""
    try:
        if PSUTIL_AVAILABLE:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd']):
                try:
                    cmdline = proc.info.get('cmdline', [])
                    if cmdline:
                        # Prüfe alle cmdline-Argumente als String (für bash-Wrapper)
                        cmdline_str = ' '.join(str(arg) for arg in cmdline)
                        # Prüfe auf app.py - entweder mit "dashboard" im Pfad oder im dashboard-Verzeichnis
                        if 'app.py' in cmdline_str:
                            # Prüfe ob es im dashboard-Verzeichnis läuft (cwd) oder "dashboard" im Pfad hat
                            cwd = proc.info.get('cwd', '')
                            if 'dashboard' in cmdline_str.lower() or 'dashboard' in str(cwd).lower():
                                return True
                            # Fallback: Wenn app.py direkt aufgerufen wird (ohne Pfad), prüfe cwd
                            if any('app.py' in str(arg) and '/' not in str(arg) for arg in cmdline):
                                # Prüfe ob cwd dashboard-Verzeichnis ist
                                if 'dashboard' in str(cwd).lower():
                                    return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        else:
            # Fallback: pgrep
            result = subprocess.run(
                ['pgrep', '-f', 'python.*app.py'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return True
    except Exception as e:
        # Debug: Log error if needed
        pass
    
    return False


def get_all_services_status() -> dict:
    """Get status of all critical services. Enthält hedge_guard, hedge_guard_main, hedge_guard_sub, dashboard."""
    try:
        services = {
            "hedge_guard": {
                "name": None,
                "display_name": "Hedge Guard",
                "description": "Überwacht Hedge-Ratio (Main/Sub) und löst bei Verletzung Notmaßnahmen",
                "check_function": is_hedge_guardian_running
            },
            "dashboard": {
                "name": "hedgebot-dashboard.service",
                "display_name": "Dashboard",
                "description": "Web-Interface für Bot-Verwaltung",
                "check_function": is_dashboard_running
            }
        }

        status = {}
        all_active = True

        # Hedge Guardian pro Scope (Long/Main, Short/Sub) mit Symbol
        for scope, label in (("main", "Long (Main)"), ("sub", "Short (Sub)")):
            scope_status = get_hedge_guardian_scope_status(scope)
            status[f"hedge_guard_{scope}"] = {
                "display_name": label,
                "description": f"Guardian für {label} – Coin: {scope_status.get('symbol') or '–'}" if scope_status.get("symbol") else f"Guardian für {label}",
                "active": scope_status.get("active", False),
                "enabled": False,
                "status": "active" if scope_status.get("active") else "inactive",
                "symbol": scope_status.get("symbol"),
            }
            if not scope_status.get("active"):
                all_active = False
        
        for key, service_info in services.items():
            try:
                if service_info["name"]:
                    # Systemd service - prüfe zuerst systemd, dann Prozess als Fallback
                    service_status = check_service_status(service_info["name"])
                    is_active = service_status["active"]
                    
                    # Fallback: Wenn systemd Service nicht läuft, prüfe Prozess
                    if not is_active and service_info.get("check_function"):
                        is_active = service_info["check_function"]()
                        if is_active:
                            # Prozess läuft, aber systemd Service nicht
                            service_status = {
                                "active": True,
                                "enabled": service_status.get("enabled", False),
                                "status": "active"
                            }
                elif service_info.get("check_function"):
                    # Process check
                    is_active = service_info["check_function"]()
                    service_status = {
                        "active": is_active,
                        "enabled": False,  # Processes don't have enabled state
                        "status": "active" if is_active else "inactive"
                    }
                else:
                    is_active = False
                    service_status = {
                        "active": False,
                        "enabled": False,
                        "status": "unknown"
                    }
                
                status[key] = {
                    "display_name": service_info["display_name"],
                    "description": service_info["description"],
                    "active": is_active,
                    "enabled": service_status.get("enabled", False),
                    "status": service_status["status"]
                }
                if not is_active:
                    all_active = False
            except Exception as e:
                # If checking one service fails, mark it as inactive
                status[key] = {
                    "display_name": service_info.get("display_name", key),
                    "description": service_info.get("description", ""),
                    "active": False,
                    "enabled": False,
                    "status": "error",
                    "error": str(e)
                }
                all_active = False
        
        return {
            "services": status,
            "all_active": all_active
        }
    except Exception as e:
        # Return empty status on error
        return {
            "services": {},
            "all_active": False,
            "error": str(e)
        }


def load_bot_state(symbol: str, bot_type: str = "long") -> dict:
    """Load bot state from long_bot_state_{SYMBOL}.json or short_bot_state_{SYMBOL}.json"""
    project_dir = _get_project_root()
    
    # State file name depends on bot type
    if bot_type == "short":
        state_filename = f"short_bot_state_{symbol}.json"
    else:
        state_filename = f"long_bot_state_{symbol}.json"
    # Prefer current state location under data/state
    state_file = project_dir / "data" / "state" / state_filename
    legacy_state_file = project_dir / state_filename
    
    # Load config to get burns_before_rebuy if not in state
    config = load_config(bot_type=bot_type)
    default_burns_before_rebuy = config.get("burns_before_rebuy", 4)
    
    if not state_file.exists() and legacy_state_file.exists():
        state_file = legacy_state_file

    if not state_file.exists():
        return {
            "burn_count": 0,
            "burns_before_rebuy": default_burns_before_rebuy,
            "total_burned": 0.0,
            "cycle_progress": 0,
            "next_rebuy_in": default_burns_before_rebuy
        }
    
    try:
        with open(state_file, 'r') as f:
            state = json.load(f)

            # Burn-Anzeige: Pro-Symbol-State (short_bot_state_SYMBOL.json) hat Priorität,
            # damit pro Symbol der aktuelle Zyklus (z.B. 1/2) korrekt angezeigt wird.
            # account_state_* wird nur als Fallback genutzt, falls im State-File nichts steht.
            account_scope = "main" if bot_type != "short" else "sub"
            account_state_file = project_dir / "data" / "state" / f"account_state_{account_scope}.json"
            if account_state_file.exists():
                try:
                    with open(account_state_file, "r") as af:
                        account_state = json.load(af) or {}
                    # burn_count/total_burned: zuerst aus State-File (pro Symbol), sonst Account
                    state["burn_count"] = int(
                        state.get("burn_count", account_state.get("burn_count", 0)) or 0
                    )
                    state["total_burned"] = float(
                        state.get("total_burned", account_state.get("total_burned", 0.0)) or 0.0
                    )
                    state["lifetime_burn_count"] = int(
                        state.get(
                            "lifetime_burn_count",
                            account_state.get("lifetime_burn_count", state.get("burn_count", 0)),
                        ) or 0
                    )
                    state["lifetime_total_burned"] = float(
                        state.get(
                            "lifetime_total_burned",
                            account_state.get("lifetime_total_burned", state.get("total_burned", 0.0)),
                        ) or 0.0
                    )
                except Exception:
                    pass

            # Get burns_before_rebuy from state or config
            burns_before_rebuy = state.get("burns_before_rebuy", default_burns_before_rebuy)
            state["burns_before_rebuy"] = burns_before_rebuy
            
            # Calculate cycle progress
            burn_count = state.get("burn_count", 0)
            cycle_progress = int((burn_count / burns_before_rebuy) * 100) if burns_before_rebuy > 0 else 0
            next_rebuy_in = max(0, burns_before_rebuy - burn_count)
            
            state["cycle_progress"] = cycle_progress
            state["next_rebuy_in"] = next_rebuy_in
            return state
    except:
        return {
            "burn_count": 0,
            "burns_before_rebuy": default_burns_before_rebuy,
            "total_burned": 0.0,
            "cycle_progress": 0,
            "next_rebuy_in": default_burns_before_rebuy
        }

