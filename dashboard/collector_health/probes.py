"""Read-only process / HTTP / filesystem probes (no start/stop)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ORDERBOOK_ROOT = Path(
    os.environ.get(
        "ORDERBOOK_ANALYSE_ROOT",
        "/home/telgenbuescher/projects/orderbook_analyse",
    )
)
OI_PID_PATH = ORDERBOOK_ROOT / "logs" / "oi_liquidation_collector.pid"
OB_RAW_HEALTH = ORDERBOOK_ROOT / "logs" / "orderbook_v3_raw_archive_btc_doge.health.ndjson"
OB_RAW_LOCK = ORDERBOOK_ROOT / "logs" / "orderbook_v3_raw_archive_only.lock"
STOCH_API = os.environ.get("STOCH_COLLECTOR_API_BASE", "http://127.0.0.1:8787").rstrip("/")


def _read_pid_file(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        return int(text)
    except (OSError, ValueError, IndexError):
        return None


def _cmdline(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.replace(b"\0", b" ").decode("utf-8", errors="replace")
    except OSError:
        return None


def _proc_start_iso(pid: int) -> str | None:
    try:
        # /proc/pid/stat field 22 = starttime (ticks); use status for simplicity via lstart unavailable
        st = Path(f"/proc/{pid}").stat()
        # Not exact process start; approximate via /proc creation — use better: stat on /proc/pid
        return datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except OSError:
        return None


def find_pids_by_needle(needle: str) -> list[int]:
    found: list[int] = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                    "utf-8", errors="replace"
                )
            except OSError:
                continue
            if needle in cmd:
                found.append(int(entry.name))
    except OSError:
        pass
    return found


def probe_oi_process() -> dict[str, Any]:
    pid = _read_pid_file(OI_PID_PATH)
    running = False
    cmdline = None
    if pid is not None:
        cmdline = _cmdline(pid)
        running = bool(cmdline and "oi_liquidation_collector" in cmdline)
    if not running:
        alts = find_pids_by_needle("oi_liquidation_collector")
        if alts:
            pid = alts[0]
            cmdline = _cmdline(pid)
            running = True
    return {
        "process_running": running,
        "pid": pid if running else None,
        "process_started_at": _proc_start_iso(pid) if running and pid else None,
        "cmdline": cmdline,
    }


def probe_full_ob_raw() -> dict[str, Any]:
    pids = find_pids_by_needle("orderbook_v3_raw_archive")
    pids = [p for p in pids if "30d_import" not in (_cmdline(p) or "")]
    last_state = None
    last_connected = None
    last_error = None
    if OB_RAW_HEALTH.is_file():
        try:
            with OB_RAW_HEALTH.open("rb") as f:
                f.seek(max(0, OB_RAW_HEALTH.stat().st_size - 120000))
                lines = f.read().decode("utf-8", "replace").splitlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                last_state = obj.get("collector_state")
                last_connected = obj.get("connected")
                last_error = obj.get("last_error")
                break
        except OSError:
            pass
    running = bool(pids)
    return {
        "process_running": running,
        "pid": pids[0] if pids else None,
        "process_started_at": _proc_start_iso(pids[0]) if pids else None,
        "health_state": last_state,
        "connected": last_connected,
        "last_error": last_error,
        "lock_present": OB_RAW_LOCK.is_file(),
    }


def probe_stoch_status(*, timeout_s: float = 3.0) -> dict[str, Any]:
    url = f"{STOCH_API}/api/collector/status"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "collector-health/1"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "data": data}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "error": str(exc)[:300], "data": None}


def probe_stoch_process() -> dict[str, Any]:
    pids = find_pids_by_needle("run_live_collector_service.py")
    running = bool(pids)
    return {
        "process_running": running,
        "pid": pids[0] if pids else None,
        "process_started_at": _proc_start_iso(pids[0]) if pids else None,
    }
