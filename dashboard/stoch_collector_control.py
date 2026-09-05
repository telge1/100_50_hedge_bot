"""Start/stop/restart the Stoch live collector (Candles + optional Public).

Hard rules:
- Never touch OB V3 live, OI/Liq, or OB 30d import processes.
- Always start with --candle-universe so startup RECOVERING fills candle gaps.
- Public live requires --enable-public-trades (process restart).
- Enabling Public also kicks a short archive gap backfill for recent UTC days
  so closed days are not left empty while live catches up from "now".
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from live_feed_status import live_feeds_overview

STOCH_ROOT = Path(
    os.environ.get(
        "STOCH_COLLECTOR_ROOT",
        "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves",
    )
)
STOCH_PYTHON = Path(
    os.environ.get("STOCH_COLLECTOR_PYTHON", str(STOCH_ROOT / ".venv" / "bin" / "python"))
)
STATE_DIR = Path(
    os.environ.get(
        "STOCH_COLLECTOR_STATE_DIR",
        str(STOCH_ROOT / "results" / "live_collector"),
    )
)
PID_PATH = STATE_DIR / "collector_service.pid"
LOCK_PATH = STATE_DIR / "collector.lock"
LOG_PATH = STATE_DIR / "collector_service.nohup.log"
DESIRED_PATH = STATE_DIR / "desired_state.json"
PUBLIC_GAP_PID = STATE_DIR / "public_gap_backfill.pid"
PUBLIC_GAP_LOG = STATE_DIR / "public_gap_backfill.nohup.log"
PUBLIC_GAP_META = STATE_DIR / "public_gap_backfill.json"
PREFERRED_MODE_PATH = STATE_DIR / "preferred_mode.json"
PUBLIC_GAP_LOOKBACK_DAYS = int(os.environ.get("STOCH_PUBLIC_GAP_LOOKBACK_DAYS", "3"))
API_BASE = os.environ.get("STOCH_COLLECTOR_API_BASE", "http://127.0.0.1:8787").rstrip("/")

CMDLINE_NEEDLE = "run_live_collector_service.py"
FORBIDDEN_CMDLINE = (
    "orderbook_v2_live",
    "oi_liquidation_collector",
    "run_orderbook_v3_30d_import",
)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if raw.startswith("pid="):
            raw = raw.split("=", 1)[1].strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"pid={pid}\n", encoding="utf-8")


def _proc_cmdline(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return None


def _is_stoch_cmdline(cmdline: str | None) -> bool:
    if not cmdline:
        return False
    if CMDLINE_NEEDLE not in cmdline:
        return False
    if any(bad in cmdline for bad in FORBIDDEN_CMDLINE):
        return False
    return True


def find_stoch_pid() -> int | None:
    """Prefer pid file, fall back to pgrep."""
    pid = _read_pid(PID_PATH)
    if pid is not None:
        cmd = _proc_cmdline(pid)
        if _is_stoch_cmdline(cmd):
            return pid
    try:
        proc = subprocess.run(
            ["pgrep", "-f", CMDLINE_NEEDLE],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    for line in proc.stdout.splitlines():
        try:
            cand = int(line.strip())
        except ValueError:
            continue
        if _is_stoch_cmdline(_proc_cmdline(cand)):
            return cand
    return None


def public_enabled_in_cmdline(cmdline: str | None) -> bool:
    return bool(cmdline and "--enable-public-trades" in cmdline)


def candle_universe_in_cmdline(cmdline: str | None) -> bool:
    return bool(cmdline and "--candle-universe" in cmdline)


def fetch_api_status(*, timeout: float = 3.0) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{API_BASE}/api/collector/status")
        data = resp.json()
        return data if isinstance(data, dict) else {"error": "invalid_json"}
    except Exception as exc:  # noqa: BLE001
        return {"error": "collector_api_unreachable", "detail": str(exc)}


def set_desired_state(desired: str, *, timeout: float = 5.0) -> dict[str, Any]:
    desired = str(desired).upper()
    if desired not in ("RUNNING", "STOPPED"):
        return {"ok": False, "error": "invalid_desired_state"}
    # Persist locally even if API is down (next start reads file).
    try:
        DESIRED_PATH.parent.mkdir(parents=True, exist_ok=True)
        DESIRED_PATH.write_text(
            json.dumps(
                {
                    "desired_state": desired,
                    "reason": "dashboard_service_control",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return {"ok": False, "error": "desired_state_write_failed", "detail": str(exc)}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{API_BASE}/api/collector/desired_state",
                json={"desired_state": desired},
            )
        body: Any
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"raw": resp.text[:300]}
        return {"ok": resp.status_code < 400, "http_status": resp.status_code, "body": body}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": True,
            "http_status": None,
            "warning": "api_unreachable_file_written",
            "detail": str(exc),
        }


def _save_preferred_mode(*, public_trades: bool) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PREFERRED_MODE_PATH.write_text(
            json.dumps(
                {
                    "public_trades": bool(public_trades),
                    "candle_universe": True,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _load_preferred_public() -> bool:
    try:
        raw = json.loads(PREFERRED_MODE_PATH.read_text(encoding="utf-8"))
        return bool(raw.get("public_trades"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def build_stoch_argv(*, public_trades: bool) -> list[str]:
    """Canonical production argv — always candle-universe for gap recovery."""
    argv = [
        str(STOCH_PYTHON),
        "scripts/run_live_collector_service.py",
        "--live-universe",
        "config/live_universe.json",
        "--candle-universe",
        "config/universe_tradeable_51.json",
        "--api-host",
        "127.0.0.1",
        "--api-port",
        "8787",
        "--default-desired",
        "RUNNING",
    ]
    if public_trades:
        argv.append("--enable-public-trades")
        argv.extend(
            [
                "--public-trade-queue-maxsize",
                "100000",
                "--public-trade-batch-size",
                "2000",
                "--public-trade-spool-dir",
                "results/live_collector/public_trade_spool",
            ]
        )
    return argv


def stop_stoch(*, timeout_s: float = 45.0) -> dict[str, Any]:
    set_desired_state("STOPPED")
    pid = find_stoch_pid()
    if pid is None:
        return {"ok": True, "stopped": False, "reason": "not_running"}
    cmdline = _proc_cmdline(pid)
    if not _is_stoch_cmdline(cmdline):
        return {"ok": False, "error": "pid_cmdline_mismatch", "pid": pid}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"ok": True, "stopped": True, "pid": pid, "reason": "already_dead"}
    except PermissionError as exc:
        return {"ok": False, "error": "permission_denied", "detail": str(exc), "pid": pid}

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _proc_cmdline(pid) is None:
            return {"ok": True, "stopped": True, "pid": pid, "signal": "SIGTERM"}
        time.sleep(0.4)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return {"ok": True, "stopped": True, "pid": pid, "signal": "SIGTERM"}
    time.sleep(0.5)
    alive = _proc_cmdline(pid) is not None
    return {
        "ok": not alive,
        "stopped": not alive,
        "pid": pid,
        "signal": "SIGKILL",
        "error": None if not alive else "still_alive_after_sigkill",
    }


def start_stoch(*, public_trades: bool) -> dict[str, Any]:
    existing = find_stoch_pid()
    if existing is not None:
        cmd = _proc_cmdline(existing) or ""
        already_public = public_enabled_in_cmdline(cmd)
        if already_public == bool(public_trades) and candle_universe_in_cmdline(cmd):
            set_desired_state("RUNNING")
            return {
                "ok": True,
                "started": False,
                "reason": "already_running_matching_mode",
                "pid": existing,
                "public_trades": already_public,
                "candle_backfill": "RECOVERING_ON_NEXT_RESTART_ONLY",
            }
        return {
            "ok": False,
            "error": "already_running_different_mode",
            "detail": "Use restart to change public_trades / force candle recovery",
            "pid": existing,
            "public_trades": already_public,
        }

    if not STOCH_PYTHON.is_file():
        return {"ok": False, "error": "python_missing", "path": str(STOCH_PYTHON)}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    set_desired_state("RUNNING")
    argv = build_stoch_argv(public_trades=public_trades)
    log_fh = LOG_PATH.open("a", encoding="utf-8")
    try:
        log_fh.write(
            f"\n===== STOCH_START utc={datetime.now(timezone.utc).isoformat()} "
            f"public_trades={public_trades} argv={' '.join(argv)} =====\n"
        )
        log_fh.flush()
        proc = subprocess.Popen(
            argv,
            cwd=str(STOCH_ROOT),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_fh.close()
    _write_pid(PID_PATH, proc.pid)
    _save_preferred_mode(public_trades=public_trades)
    # Brief wait — API may come up during RECOVERING.
    time.sleep(1.0)
    return {
        "ok": True,
        "started": True,
        "pid": proc.pid,
        "public_trades": bool(public_trades),
        "candle_backfill": "AUTO_RECOVERING_ON_START",
        "argv": argv,
        "log_path": str(LOG_PATH),
    }


def restart_stoch(*, public_trades: bool) -> dict[str, Any]:
    stop = stop_stoch()
    if not stop.get("ok"):
        return {"ok": False, "error": "stop_failed", "stop": stop}
    # Drop stale lock if present and process is gone.
    if LOCK_PATH.is_file() and find_stoch_pid() is None:
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass
    start = start_stoch(public_trades=public_trades)
    return {"ok": bool(start.get("ok")), "stop": stop, "start": start}


def _public_gap_running() -> dict[str, Any]:
    pid = _read_pid(PUBLIC_GAP_PID)
    cmdline = _proc_cmdline(pid) if pid else None
    running = bool(
        cmdline
        and "run_public_trades_7d_backfill.py" in cmdline
        and "orderbook_v2_live" not in cmdline
    )
    meta: dict[str, Any] = {}
    if PUBLIC_GAP_META.is_file():
        try:
            meta = json.loads(PUBLIC_GAP_META.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
    return {
        "running": running,
        "pid": pid if running else None,
        "meta": meta,
        "log_path": str(PUBLIC_GAP_LOG),
    }


def start_public_gap_backfill(*, lookback_days: int | None = None) -> dict[str, Any]:
    """Archive backfill for recent complete UTC days (idempotent re-import safe enough).

    Live public WS only covers from connect forward. Closed calendar days since the
    last live window must come from archive to avoid gaps.
    """
    days = int(lookback_days if lookback_days is not None else PUBLIC_GAP_LOOKBACK_DAYS)
    days = max(1, min(days, 14))
    existing = _public_gap_running()
    if existing["running"]:
        return {"ok": True, "started": False, "reason": "already_running", **existing}

    end_excl = _utc_today()
    start = end_excl - timedelta(days=days)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (
        STOCH_ROOT
        / "results"
        / "public_trades_gap_backfill"
        / run_id
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        str(STOCH_PYTHON),
        "scripts/run_public_trades_7d_backfill.py",
        "--mode",
        "backfill",
        "--start-date",
        start.isoformat(),
        "--end-date-exclusive",
        end_excl.isoformat(),
        "--universe-file",
        "config/universe_tradeable_51.json",
        "--workers",
        "1",
        "--run-dir",
        str(run_dir),
    ]
    meta = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "start_date": start.isoformat(),
        "end_date_exclusive": end_excl.isoformat(),
        "lookback_days": days,
        "run_dir": str(run_dir),
        "argv": argv,
        "note": (
            "Archive gap fill for closed UTC days. Live WS covers from collector "
            "connect forward. Candles use collector RECOVERING separately."
        ),
    }
    PUBLIC_GAP_META.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    log_fh = PUBLIC_GAP_LOG.open("a", encoding="utf-8")
    try:
        log_fh.write(
            f"\n===== PUBLIC_GAP_START utc={meta['started_at']} "
            f"window=[{start.isoformat()},{end_excl.isoformat()}) =====\n"
        )
        log_fh.flush()
        proc = subprocess.Popen(
            argv,
            cwd=str(STOCH_ROOT),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_fh.close()
    _write_pid(PUBLIC_GAP_PID, proc.pid)
    meta["pid"] = proc.pid
    PUBLIC_GAP_META.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "started": True, "pid": proc.pid, **meta}


def services_overview() -> dict[str, Any]:
    pid = find_stoch_pid()
    cmdline = _proc_cmdline(pid) if pid else None
    api = fetch_api_status()
    public_on = bool(api.get("public_trades_enabled")) or public_enabled_in_cmdline(cmdline)
    feeds = live_feeds_overview()
    gap = _public_gap_running()
    collector_state = api.get("collector_state") or api.get("state") or (
        "RUNNING" if pid else "STOPPED"
    )
    return {
        "stoch": {
            "id": "stoch",
            "label": "Candles + Signals",
            "running": pid is not None,
            "pid": pid,
            "collector_state": collector_state,
            "desired_state": api.get("desired_state"),
            "public_trades_enabled": public_on,
            "candle_universe": candle_universe_in_cmdline(cmdline)
            or bool(api.get("candle_symbols")),
            "candle_count": len(api.get("candle_symbols") or []),
            "signal_count": len(api.get("signal_symbols") or []),
            "backfill_on_start": "AUTO_RECOVERING (FILL_MISSING_RANGES)",
            "cmdline": (cmdline or "")[:300] if cmdline else None,
            "api_error": api.get("error"),
        },
        "public": {
            "id": "public",
            "label": "Public Trades live",
            "enabled": public_on,
            "symbol_count": len(api.get("public_trade_symbols") or []) if public_on else 0,
            "gap_backfill": gap,
            "backfill_on_enable": (
                f"archive gap lookback {PUBLIC_GAP_LOOKBACK_DAYS}d + live WS from connect"
            ),
        },
        "orderbook_live": {
            **feeds.get("orderbook_live", {}),
            "id": "orderbook_live",
            "label": "OB V3 Live",
            "controllable": True,
            "backfill_on_start": "collector snapshot/recovery on connect (not 30d import)",
            "note": "universe51 live only",
        },
        "oi_liquidation": {
            **feeds.get("oi_liquidation", {}),
            "id": "oi_liquidation",
            "label": "OI/Liq Live",
            "controllable": True,
            "backfill_on_start": "live from connect",
            "note": "51-symbol live; no OB 30d",
        },
        "canonical_argv_public_on": build_stoch_argv(public_trades=True),
        "canonical_argv_public_off": build_stoch_argv(public_trades=False),
    }


def apply_service_action(
    service_id: str,
    action: str,
    *,
    confirm: bool = False,
    public_gap_lookback_days: int | None = None,
) -> dict[str, Any]:
    """Dashboard control plane for Stoch/Public/OB/OI. Never OB 30d import."""
    from ob_oi_collector_control import apply_ob_oi_action

    sid = str(service_id or "").strip().lower()
    act = str(action or "").strip().lower()
    if sid in (
        "orderbook_live",
        "ob",
        "ob_live",
        "ob_v3",
        "oi_liquidation",
        "oi",
        "oi_liq",
        "import",
        "ob_import",
        "orderbook_30d",
        "ob_30d",
    ):
        return apply_ob_oi_action(sid, act, confirm=confirm)
    if sid in ("stoch", "candles", "signals"):
        if act == "start":
            # Keep current public flag if process exists; else start with public off
            # unless caller uses public service.
            pid = find_stoch_pid()
            public = public_enabled_in_cmdline(_proc_cmdline(pid)) if pid else False
            if pid and not candle_universe_in_cmdline(_proc_cmdline(pid)):
                if not confirm:
                    return {
                        "ok": False,
                        "error": "confirm_required",
                        "detail": (
                            "Running without --candle-universe. Restart required for "
                            "gap-free candle recovery."
                        ),
                    }
                return restart_stoch(public_trades=public)
            if pid:
                des = set_desired_state("RUNNING")
                return {"ok": True, "action": "desired_RUNNING", "desired": des, "pid": pid}
            # Dead process: restore last preferred mode (default public off until once enabled).
            return start_stoch(public_trades=_load_preferred_public())
        if act == "stop":
            if not confirm:
                return {
                    "ok": False,
                    "error": "confirm_required",
                    "detail": "Stopping Stoch collector stops candles + signals (+ public if on).",
                }
            return stop_stoch()
        if act == "restart":
            if not confirm:
                return {"ok": False, "error": "confirm_required"}
            pid = find_stoch_pid()
            public = public_enabled_in_cmdline(_proc_cmdline(pid)) if pid else False
            return restart_stoch(public_trades=public)
        return {"ok": False, "error": "unknown_action", "action": act}

    if sid in ("public", "public_trades"):
        if act in ("start", "enable"):
            if not confirm:
                return {
                    "ok": False,
                    "error": "confirm_required",
                    "detail": (
                        "Enables Public via Stoch restart with --enable-public-trades, "
                        "triggers candle RECOVERING, and starts archive gap backfill "
                        f"for the last {PUBLIC_GAP_LOOKBACK_DAYS} UTC days."
                    ),
                }
            restart = restart_stoch(public_trades=True)
            gap = start_public_gap_backfill(lookback_days=public_gap_lookback_days)
            return {
                "ok": bool(restart.get("ok")),
                "restart": restart,
                "public_gap_backfill": gap,
                "candle_backfill": "AUTO_RECOVERING_ON_START",
            }
        if act in ("stop", "disable"):
            if not confirm:
                return {
                    "ok": False,
                    "error": "confirm_required",
                    "detail": (
                        "Disables Public via Stoch restart without --enable-public-trades. "
                        "Candles stay on with RECOVERING."
                    ),
                }
            return restart_stoch(public_trades=False)
        return {"ok": False, "error": "unknown_action", "action": act}

    return {"ok": False, "error": "unknown_service", "service_id": sid}
