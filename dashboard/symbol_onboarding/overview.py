"""Lightweight symbol overview from config SoT + optional job cache (no heavy CH)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import OVERVIEW_CACHE_TTL_SEC, jobs_dir, oa_root

_cache: dict[str, Any] = {"expires": 0.0, "payload": None}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _symbols_from_universe(path: Path) -> list[str]:
    data = _load_json(path)
    raw = data.get("symbols") or []
    if not isinstance(raw, list):
        return []
    return [str(s).strip().upper() for s in raw if str(s).strip()]


def build_overview(*, environ: dict | None = None, use_cache: bool = True) -> dict[str, Any]:
    now = time.time()
    if use_cache and _cache["payload"] is not None and now < float(_cache["expires"]):
        return _cache["payload"]

    root = oa_root(environ)
    universe_path = root / "config" / "universe_tradeable_51.json"
    ob_path = root / "config" / "ob1000_live_symbols.json"
    tick_path = root / "config" / "ob1000_tick_sizes.json"

    universe = _symbols_from_universe(universe_path)
    ob_syms = set(_symbols_from_universe(ob_path))
    ticks = (_load_json(tick_path).get("tick_sizes") or {}) if tick_path.is_file() else {}
    if not isinstance(ticks, dict):
        ticks = {}

    # Recent apply jobs for status hints (bounded)
    recent: dict[str, dict[str, Any]] = {}
    for path in list(jobs_dir(environ).glob("*.json"))[:200]:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(job, dict) or not job.get("apply"):
            continue
        sym = str(job.get("symbol") or "").upper()
        if not sym:
            continue
        prev = recent.get(sym)
        if prev is None or str(job.get("updated_at") or "") > str(prev.get("updated_at") or ""):
            recent[sym] = job

    rows = []
    for symbol in universe:
        job = recent.get(symbol) or {}
        status = "READY"
        job_status = str(job.get("status") or "")
        if job_status == "RUNNING":
            status = "BACKFILL_RUNNING"
        elif job_status == "WAITING_FOR_ARCHIVE":
            status = "WAITING_FOR_ARCHIVE"
        elif job_status == "PARTIAL_LIVE_ONLY":
            status = "LIVE_ONLY"
        elif job_status == "FAILED":
            status = "FAILED"
        elif job_status == "ROLLED_BACK":
            status = "ROLLED_BACK"
        elif job_status == "SUCCEEDED" and not job.get("with_ob1000"):
            status = "READY"
        elif symbol in ob_syms:
            status = "READY"
        streams = []
        streams.append("candles_1m")
        if symbol in ob_syms:
            streams.extend(["ob1000", "open_interest_5s", "liquidations"])
        rows.append(
            {
                "symbol": symbol,
                "streams": streams,
                "tick_size": ticks.get(symbol),
                "ob1000_registered": symbol in ob_syms,
                "raw_status": "REGISTERED" if symbol in ob_syms else "NOT_REGISTERED",
                "materializer_status": "UNKNOWN",
                "clickhouse_status": "UNKNOWN",
                "last_data_at": None,
                "live_since": job.get("created_at") if job.get("restart_live") else None,
                "history_days": job.get("requested_days"),
                "overall_status": status,
                "last_job_id": job.get("job_id"),
                "last_job_status": job_status or None,
            }
        )

    payload = {
        "success": True,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "universe_path": str(universe_path),
        "count": len(rows),
        "symbols": rows,
        "note": (
            "Übersicht aus Universe-/OB1000-SoT und letzten Jobdateien. "
            "Keine teuren ClickHouse-Vollscans."
        ),
    }
    _cache["payload"] = payload
    _cache["expires"] = now + OVERVIEW_CACHE_TTL_SEC
    return payload


def clear_overview_cache() -> None:
    _cache["payload"] = None
    _cache["expires"] = 0.0
