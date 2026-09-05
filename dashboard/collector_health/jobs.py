"""In-memory + disk audit log for gated backfill jobs (no shell=True)."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import PUBLIC_TRADES_BACKFILL_GATE
from .oi_backfill import MAX_UI_SPAN_DAYS, normalize_symbol, parse_utc, run_backfill

ALLOWED_JOB_KINDS = frozenset({"oi_5m_detect", "oi_5m_backfill_dry_run", "oi_5m_backfill_execute"})
# Execute remains fail-closed unless env flag set — default dry-run for UI start
import os

ALLOW_OI_EXECUTE = os.environ.get("COLLECTOR_HEALTH_ALLOW_OI_EXECUTE", "").strip() == "1"

JOB_DIR = Path(
    os.environ.get(
        "COLLECTOR_HEALTH_JOB_DIR",
        "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/collector_health_jobs",
    )
)

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_oi_running = False


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _audit(event: dict[str, Any]) -> None:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    path = JOB_DIR / "audit.ndjson"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def validate_backfill_request(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    kind = str(body.get("job_kind") or body.get("action") or "").strip()
    collector_id = str(body.get("collector_id") or "").strip()
    if collector_id == "public_trades_live" or kind.startswith("public_trades"):
        return None, f"PUBLIC_TRADES_BLOCKED:{PUBLIC_TRADES_BACKFILL_GATE}"
    if collector_id and collector_id not in ("oi_5m_history", "oi_liquidation_live"):
        if collector_id != "oi_5m_history":
            return None, "INVALID_COLLECTOR_ID"
    if kind in ("detect", "oi_5m_detect"):
        kind = "oi_5m_detect"
    elif kind in ("start", "backfill", "oi_5m_backfill_dry_run"):
        kind = "oi_5m_backfill_dry_run"
    elif kind in ("execute", "oi_5m_backfill_execute"):
        kind = "oi_5m_backfill_execute"
    else:
        return None, "INVALID_JOB_KIND"
    if kind not in ALLOWED_JOB_KINDS:
        return None, "INVALID_JOB_KIND"
    if kind == "oi_5m_backfill_execute" and not ALLOW_OI_EXECUTE:
        return None, "OI_EXECUTE_FAIL_CLOSED"
    try:
        symbols = [normalize_symbol(s) for s in (body.get("symbols") or [])]
    except ValueError as exc:
        return None, f"INVALID_SYMBOL:{exc}"
    if not symbols:
        return None, "SYMBOLS_REQUIRED"
    try:
        start = parse_utc(body["start"])
        end = parse_utc(body["end"])
    except Exception:
        return None, "INVALID_TIME_RANGE"
    if end < start:
        return None, "INVALID_TIME_RANGE"
    span_days = (end - start).total_seconds() / 86400.0
    if span_days > MAX_UI_SPAN_DAYS:
        return None, f"SPAN_EXCEEDS_MAX_DAYS_{MAX_UI_SPAN_DAYS}"
    return {
        "job_kind": kind,
        "collector_id": "oi_5m_history",
        "symbols": symbols,
        "start": start,
        "end": end,
    }, None


def start_job(*, body: dict[str, Any], user: str) -> tuple[dict[str, Any], int]:
    global _oi_running
    parsed, err = validate_backfill_request(body)
    if err:
        _audit({"ts": _utc(), "user": user, "action": "reject", "error": err, "body_keys": list(body.keys())})
        code = 409 if "BLOCKED" in err or "FAIL_CLOSED" in err else 400
        return {"success": False, "error": err}, code

    assert parsed is not None
    with _lock:
        if _oi_running:
            return {"success": False, "error": "OI_BACKFILL_LOCK_HELD"}, 409
        _oi_running = True
        job_id = str(uuid.uuid4())
        job = {
            "job_id": job_id,
            "job_kind": parsed["job_kind"],
            "collector_id": parsed["collector_id"],
            "symbols": parsed["symbols"],
            "start": parsed["start"].isoformat().replace("+00:00", "Z"),
            "end": parsed["end"].isoformat().replace("+00:00", "Z"),
            "status": "RUNNING",
            "progress": 0,
            "started_at": _utc(),
            "finished_at": None,
            "user": user,
            "result": None,
            "error": None,
            "logs": [],
        }
        _jobs[job_id] = job

    def _run() -> None:
        global _oi_running
        try:
            dry = parsed["job_kind"] != "oi_5m_backfill_execute"
            detect_only = parsed["job_kind"] == "oi_5m_detect"
            result = run_backfill(
                symbols=parsed["symbols"],
                start=parsed["start"],
                end=parsed["end"],
                dry_run=dry or detect_only,
                detect_only=detect_only,
                verify_only=False,
                run_id=job_id,
            )
            with _lock:
                job = _jobs[job_id]
                job["status"] = "COMPLETED"
                job["progress"] = 100
                job["finished_at"] = _utc()
                job["result"] = result
                job["logs"].append("completed")
            _audit(
                {
                    "ts": _utc(),
                    "user": user,
                    "action": parsed["job_kind"],
                    "job_id": job_id,
                    "symbols": parsed["symbols"],
                    "start": job["start"],
                    "end": job["end"],
                    "result_status": result.get("status"),
                    "inserted_total": result.get("inserted_total"),
                    "would_insert_total": result.get("would_insert_total"),
                }
            )
        except Exception as exc:
            with _lock:
                job = _jobs[job_id]
                job["status"] = "FAILED"
                job["finished_at"] = _utc()
                job["error"] = str(exc)[:500]
            _audit(
                {
                    "ts": _utc(),
                    "user": user,
                    "action": parsed["job_kind"],
                    "job_id": job_id,
                    "error": str(exc)[:500],
                }
            )
        finally:
            with _lock:
                _oi_running = False

    threading.Thread(target=_run, name=f"oi-backfill-{job_id[:8]}", daemon=True).start()
    return {"success": True, "job_id": job_id, "job": get_job(job_id)}, 202
