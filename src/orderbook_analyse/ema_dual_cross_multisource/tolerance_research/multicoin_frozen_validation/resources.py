"""Resource snapshot helpers (no process control; psutil optional)."""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from typing import Any


def _proc_meminfo() -> dict[str, float]:
    out: dict[str, float] = {}
    path = "/proc/meminfo"
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            parts = rest.strip().split()
            if not parts:
                continue
            try:
                kib = float(parts[0])
            except ValueError:
                continue
            out[key] = kib * 1024.0
    return out


def _fallback_resources() -> dict[str, Any]:
    snap: dict[str, Any] = {"source": "fallback_/proc_shutil"}
    mem = _proc_meminfo()
    total = mem.get("MemTotal")
    avail = mem.get("MemAvailable") or mem.get("MemFree")
    if total:
        snap["ram_total_gb"] = round(total / (1024**3), 3)
    if avail is not None and total:
        snap["ram_available_gb"] = round(avail / (1024**3), 3)
        snap["ram_percent"] = round((1.0 - avail / total) * 100.0, 2)
    try:
        du = shutil.disk_usage("/")
        snap["disk_free_gb"] = round(du.free / (1024**3), 3)
        snap["disk_percent"] = round(du.used / du.total * 100.0, 2) if du.total else None
    except Exception as exc:
        snap["disk_error"] = str(exc)
    try:
        snap["cpu_count"] = os.cpu_count()
    except Exception:
        pass
    try:
        import resource

        ru = resource.getrusage(resource.RUSAGE_SELF)
        snap["self_maxrss_mb"] = round(ru.ru_maxrss / 1024.0, 3)  # Linux: KB
    except Exception as exc:
        snap["resource_module_error"] = str(exc)
    return snap


def resource_snapshot() -> dict[str, Any]:
    snap: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat()}
    try:
        import psutil

        vm = psutil.virtual_memory()
        du = psutil.disk_usage("/")
        snap.update(
            {
                "source": "psutil",
                "cpu_percent": psutil.cpu_percent(interval=0.0),
                "cpu_count": psutil.cpu_count(),
                "ram_total_gb": round(vm.total / (1024**3), 3),
                "ram_available_gb": round(vm.available / (1024**3), 3),
                "ram_percent": vm.percent,
                "disk_free_gb": round(du.free / (1024**3), 3),
                "disk_percent": du.percent,
            }
        )
        return snap
    except Exception as exc:
        snap["psutil_error"] = str(exc)
        try:
            snap.update(_fallback_resources())
            snap["status"] = "OK_FALLBACK"
        except Exception as exc2:
            snap["status"] = "RESOURCE_METRICS_UNAVAILABLE"
            snap["fallback_error"] = str(exc2)
        return snap
