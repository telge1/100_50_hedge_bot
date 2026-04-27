"""
Bot monitoring utilities
"""
import subprocess
import json
import os
from pathlib import Path
from typing import Optional, Tuple
from .config_manager import load_config

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
    profile: main|None = nur Main-Bot (kein bot_1/bot_2); bot_1|bot_2 = profil-spezifisch.
    Der Prozess-Scan kann bot_1/bot_2 nicht zuverlässig unterscheiden – daher bei main
    Prozesse mit 'bot_1' oder 'bot_2' im Cmdline ausschließen."""
    if not PSUTIL_AVAILABLE:
        return False
    try:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return False
        script_name = f"{bot_type}_bot.py"
        prof = (profile or "").strip()
        exclude_bot_profiles = prof not in ("bot_1", "bot_2")  # Bei main: nur Main-Bot zählen
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
                    if exclude_bot_profiles and ("BOT_1" in cmd_str or "BOT_2" in cmd_str):
                        continue  # Das ist bot_1/bot_2 – für Main-Profil nicht zählen
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
    profile: bot_1|bot_2 für profil-spezifische PID-Dateien (data/run/long_bot_SYMBOL_bot_1.pid)."""
    import time
    import logging
    logger = logging.getLogger(__name__)
    service_name = f'hedgebot-{bot_type}@{symbol}'

    # First try systemd service (with one retry after 1s to avoid false "stopped" after dashboard restart)
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
    prof = (profile or "").strip()
    if prof not in ("bot_1", "bot_2"):
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

    # Primary for script-started bots: PID files in data/run (start_long_main.sh / start_short_sub.sh)
    try:
        project_dir = _get_project_root()
        run_dir = project_dir / "data" / "run"
        safe_symbol = ''.join(ch if (ch.isalnum() or ch in "_-") else '_' for ch in str(symbol or "").strip().upper())
        prof_suffix = f"_{profile}" if (profile or "").strip() in ("bot_1", "bot_2") else ""
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

    # Final fallback: scan running processes (profil-aware – bei main: bot_1/bot_2 ausschließen)
    if prof not in ("bot_1", "bot_2"):
        try:
            if _is_matching_bot_process(symbol, bot_type, profile=prof or None):
                return True
        except Exception:
            pass

    return False


def get_bot_status(symbol: str, bot_type: str = "long") -> dict:
    """Get bot status"""
    return {
        "symbol": symbol,
        "bot_type": bot_type,
        "running": is_bot_running(symbol, bot_type=bot_type),
        "service_name": f"hedgebot-{bot_type}@{symbol}"
    }


def get_bot_pid_from_run_dir(symbol: str, bot_type: str) -> Tuple[Optional[int], Optional[Path]]:
    """
    Find a running bot via PID files in data/run.
    Checks: {bot_type}_bot_{symbol}.pid, {bot_type}_bot_{symbol}_bot_1.pid, {bot_type}_bot_{symbol}_bot_2.pid.
    Returns (pid, pid_file) if found and process alive, else (None, None).
    """
    project_dir = _get_project_root()
    run_dir = project_dir / "data" / "run"
    safe_symbol = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in str(symbol or "").strip().upper())
    suffixes = ["", "_bot_1", "_bot_2"]
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
                            found_symbols.add(('long', symbol))
                            if (bot_type is None or bot_type == "long"):
                                bots.append(get_bot_status(symbol, bot_type="long"))
                
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
                                        found_symbols.add(('long', symbol))
                                        if (bot_type is None or bot_type == "long"):
                                            bots.append(get_bot_status(symbol, bot_type="long"))
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

