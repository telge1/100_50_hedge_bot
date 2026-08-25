"""Start/stop OB V3 Live and OI/Liq collectors (not OB 30d import).

Never touches:
- run_orderbook_v3_30d_import
- Stoch live collector (8787)
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from live_feed_status import ORDERBOOK_ROOT, OB_PID_PATH, OI_PID_PATH, probe_ob_live, probe_oi_liq

OB_PYTHON = Path(
    os.environ.get(
        "OB_COLLECTOR_PYTHON",
        str(ORDERBOOK_ROOT / ".venv" / "bin" / "python"),
    )
)
OB_LOG = Path(
    os.environ.get(
        "OB_V3_LIVE_LOG",
        str(ORDERBOOK_ROOT / "logs" / "orderbook_v3_live_collector.nohup.log"),
    )
)
OI_LOG = Path(
    os.environ.get(
        "OI_LIQ_LIVE_LOG",
        str(ORDERBOOK_ROOT / "logs" / "oi_liquidation_live.log"),
    )
)
OB_LOCK = ORDERBOOK_ROOT / "logs" / "orderbook_v3_live_collector.lock"
OI_LOCK = ORDERBOOK_ROOT / "logs" / "oi_liquidation_collector.lock"

FORBIDDEN = (
    "run_orderbook_v3_30d_import",
    "run_live_collector_service",
    "run_public_trades",
)


def _proc_cmdline(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return None


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")


def _is_ob_cmdline(cmdline: str | None) -> bool:
    if not cmdline:
        return False
    if "orderbook_v2_live" not in cmdline and "orderbook_v3_live" not in cmdline:
        return False
    if any(bad in cmdline for bad in FORBIDDEN):
        return False
    if "30d_import" in cmdline:
        return False
    return True


def _is_oi_cmdline(cmdline: str | None) -> bool:
    if not cmdline:
        return False
    if "oi_liquidation_collector" not in cmdline:
        return False
    if any(bad in cmdline for bad in FORBIDDEN):
        return False
    if "backfill" in cmdline and "oi_liquidation_collector" in cmdline:
        # allow module path; reject explicit backfill script entry if distinct
        if "oi_liquidation_collector.backfill" in cmdline or "backfill.py" in cmdline:
            return False
    return True


def build_ob_argv() -> list[str]:
    return [
        str(OB_PYTHON),
        "-m",
        "orderbook_analyse.orderbook_v2_live",
        "--mode",
        "universe51",
        "--confirm-universe-51",
        "--duration",
        "0",
        "--log-level",
        "INFO",
    ]


def build_oi_argv() -> list[str]:
    return [
        str(OB_PYTHON),
        "-m",
        "orderbook_analyse.oi_liquidation_collector",
        "--mode",
        "live",
        "--duration",
        "0",
        "--log-level",
        "INFO",
    ]


def _stop_pid(pid: int, *, kind: str, timeout_s: float = 45.0) -> dict[str, Any]:
    cmdline = _proc_cmdline(pid)
    ok_cmd = _is_ob_cmdline(cmdline) if kind == "ob" else _is_oi_cmdline(cmdline)
    if not ok_cmd:
        return {"ok": False, "error": "pid_cmdline_mismatch", "pid": pid, "cmdline": cmdline}
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


def stop_ob_live() -> dict[str, Any]:
    probe = probe_ob_live()
    pid = probe.get("pid")
    if not probe.get("running") or not pid:
        return {"ok": True, "stopped": False, "reason": "not_running"}
    return _stop_pid(int(pid), kind="ob")


def stop_oi_liq() -> dict[str, Any]:
    probe = probe_oi_liq()
    pid = probe.get("pid")
    if not probe.get("running") or not pid:
        return {"ok": True, "stopped": False, "reason": "not_running"}
    return _stop_pid(int(pid), kind="oi")


def start_ob_live() -> dict[str, Any]:
    probe = probe_ob_live()
    if probe.get("running"):
        return {
            "ok": True,
            "started": False,
            "reason": "already_running",
            "pid": probe.get("pid"),
            "mode": probe.get("mode"),
        }
    if not OB_PYTHON.is_file():
        return {"ok": False, "error": "python_missing", "path": str(OB_PYTHON)}
    argv = build_ob_argv()
    OB_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = OB_LOG.open("a", encoding="utf-8")
    try:
        log_fh.write(
            f"\n===== OB_V3_LIVE_START utc={datetime.now(timezone.utc).isoformat()} "
            f"argv={' '.join(argv)} =====\n"
        )
        log_fh.flush()
        proc = subprocess.Popen(
            argv,
            cwd=str(ORDERBOOK_ROOT),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONPATH": str(ORDERBOOK_ROOT / "src")},
        )
    finally:
        log_fh.close()
    _write_pid(OB_PID_PATH, proc.pid)
    time.sleep(1.0)
    return {
        "ok": True,
        "started": True,
        "pid": proc.pid,
        "argv": argv,
        "log_path": str(OB_LOG),
        "backfill": "collector_own_snapshot_recovery_on_connect",
        "note": "Does not run OB 30d import.",
    }


def start_oi_liq() -> dict[str, Any]:
    probe = probe_oi_liq()
    if probe.get("running"):
        return {
            "ok": True,
            "started": False,
            "reason": "already_running",
            "pid": probe.get("pid"),
        }
    if not OB_PYTHON.is_file():
        return {"ok": False, "error": "python_missing", "path": str(OB_PYTHON)}
    argv = build_oi_argv()
    OI_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = OI_LOG.open("a", encoding="utf-8")
    try:
        log_fh.write(
            f"\n===== OI_LIQ_LIVE_START utc={datetime.now(timezone.utc).isoformat()} "
            f"argv={' '.join(argv)} =====\n"
        )
        log_fh.flush()
        proc = subprocess.Popen(
            argv,
            cwd=str(ORDERBOOK_ROOT),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONPATH": str(ORDERBOOK_ROOT / "src")},
        )
    finally:
        log_fh.close()
    _write_pid(OI_PID_PATH, proc.pid)
    time.sleep(1.0)
    return {
        "ok": True,
        "started": True,
        "pid": proc.pid,
        "argv": argv,
        "log_path": str(OI_LOG),
        "backfill": "live_from_connect (REST OI history backfill is a separate tool)",
        "note": "Does not run OB 30d import.",
    }


def apply_ob_oi_action(service_id: str, action: str, *, confirm: bool = False) -> dict[str, Any]:
    sid = str(service_id or "").strip().lower()
    act = str(action or "").strip().lower()

    if sid in ("import", "ob_import", "orderbook_30d", "ob_30d"):
        return {
            "ok": False,
            "error": "service_not_controllable",
            "detail": "OB 30d import is one-off — no dashboard start/stop",
        }

    if sid in ("orderbook_live", "ob", "ob_live", "ob_v3"):
        if act == "start":
            if not confirm:
                return {
                    "ok": False,
                    "error": "confirm_required",
                    "detail": "Start OB V3 Live universe51 (collector recovery on connect; not 30d import).",
                }
            return start_ob_live()
        if act == "stop":
            if not confirm:
                return {
                    "ok": False,
                    "error": "confirm_required",
                    "detail": "Stop OB V3 Live collector process only.",
                }
            return stop_ob_live()
        if act == "restart":
            if not confirm:
                return {"ok": False, "error": "confirm_required"}
            stop = stop_ob_live()
            if not stop.get("ok"):
                return {"ok": False, "error": "stop_failed", "stop": stop}
            time.sleep(1.0)
            start = start_ob_live()
            return {"ok": bool(start.get("ok")), "stop": stop, "start": start}
        return {"ok": False, "error": "unknown_action", "action": act}

    if sid in ("oi_liquidation", "oi", "oi_liq"):
        if act == "start":
            if not confirm:
                return {
                    "ok": False,
                    "error": "confirm_required",
                    "detail": "Start OI/Liq live (51 symbols) until SIGTERM.",
                }
            return start_oi_liq()
        if act == "stop":
            if not confirm:
                return {
                    "ok": False,
                    "error": "confirm_required",
                    "detail": "Stop OI/Liq live collector process only.",
                }
            return stop_oi_liq()
        if act == "restart":
            if not confirm:
                return {"ok": False, "error": "confirm_required"}
            stop = stop_oi_liq()
            if not stop.get("ok"):
                return {"ok": False, "error": "stop_failed", "stop": stop}
            time.sleep(1.0)
            start = start_oi_liq()
            return {"ok": bool(start.get("ok")), "stop": stop, "start": start}
        return {"ok": False, "error": "unknown_action", "action": act}

    return {"ok": False, "error": "unknown_service", "service_id": sid}
