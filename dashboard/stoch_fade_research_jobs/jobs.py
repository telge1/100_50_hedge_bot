"""Frozen fade research job lock, create, resume, public status. No engine import."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from stoch_universe_51.coverage import coverage_report, iso_z
from stoch_universe_51.jsonio import read_json, write_json_atomic
from stoch_universe_51.origin import update_post_guard
from stoch_universe_51.universe import load_tradeable_51
from stoch_universe_51.update_jobs import active_job_id as update_job_active_id

import stoch_heavy_job_gate as heavy_gate
from .complete import SIGNAL_TFS

from .config import (
    DASHBOARD_ROOT,
    FROZEN_MODULE_HASHES,
    REPO_ROOT,
    SIDE_EFFECT_FLAGS,
    SOURCE_COMMIT,
    STRATEGY_VERSION,
    WORKER_SCRIPT,
    coin_timeout_s,
    jobs_root,
    sg_python,
    universe_path,
)
from .cross_lock import start_gate
from .disk import assert_disk
from .schema import FrozenFadeJobBody
from .symbols import filter_testable, validate_symbols
from .time_window import iso_z as window_iso
from .time_window import parse_utc_minute, suggested_end_exclusive, suggested_start, validate_window

SECRET_MARKERS = ("PASSWORD", "SECRET", "API_KEY", "TOKEN", "BYBIT_KEY", "CLICKHOUSE_PASSWORD")
JOB_ACTIVE_STATES = ("QUEUED", "RUNNING")
RESUME_STATES = ("FAILED", "INTERRUPTED", "COMPLETED_WITH_ERRORS")
WORKER_NAME = "stoch_fade_research_jobs/worker.py"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def public_message(text: str, limit: int = 180) -> str:
    raw = " ".join(str(text or "").split())
    upper = raw.upper()
    for marker in SECRET_MARKERS:
        if marker in upper:
            return "Research-Job fehlgeschlagen"
    if any(p in raw for p in ("/.env", "BEGIN RSA", "AWS_")):
        return "Research-Job fehlgeschlagen"
    return raw[:limit]


def job_dir_for(job_id: str, environ: dict | None = None) -> Path:
    return jobs_root(environ) / job_id


def lock_path(environ: dict | None = None) -> Path:
    return jobs_root(environ) / "ACTIVE.lock"


def last_job_path(environ: dict | None = None) -> Path:
    return jobs_root(environ) / "last_job_id.txt"


def _proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def worker_is_live(pid: int | None, job_id: str) -> bool:
    if not pid_alive(pid):
        return False
    cmd = _proc_cmdline(int(pid))
    return WORKER_NAME in cmd.replace("\\", "/") and job_id in cmd


def read_lock(environ: dict | None = None) -> dict[str, Any] | None:
    path = lock_path(environ)
    if not path.exists():
        return None
    try:
        return read_json(path)
    except Exception:  # noqa: BLE001
        return None


def clear_lock(environ: dict | None = None) -> None:
    path = lock_path(environ)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def reconcile_lock(environ: dict | None = None) -> dict[str, Any] | None:
    lock = read_lock(environ)
    if not lock:
        return None
    job_id = str(lock.get("job_id") or "")
    pid = lock.get("pid")
    if worker_is_live(pid if isinstance(pid, int) else None, job_id):
        return lock
    status_path = job_dir_for(job_id, environ) / "status.json"
    if status_path.exists():
        try:
            status = read_json(status_path)
        except Exception:  # noqa: BLE001
            status = {}
        if str(status.get("state")) in JOB_ACTIVE_STATES:
            status["state"] = "INTERRUPTED"
            status["finished_at"] = iso_z(_utcnow())
            status["error_summary"] = public_message("verwaister Research-Worker")
            status["message"] = status["error_summary"]
            write_json_atomic(status_path, status)
    clear_lock(environ)
    return None


def active_job_id(environ: dict | None = None) -> str | None:
    lock = reconcile_lock(environ)
    if lock:
        return str(lock.get("job_id") or "") or None
    return None


def code_revision(environ: dict | None = None) -> str:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_FADE_CODE_REVISION") or "").strip()
    if override:
        return override
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return out.decode("utf-8").strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def empty_coin_row(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "state": "PENDING",
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "runner_run_id": None,
        "raw_total": 0,
        "tier_a_total": 0,
        "per_timeframe": {
            "15m": {"raw": 0, "tier_a": 0},
            "30m": {"raw": 0, "tier_a": 0},
            "1h": {"raw": 0, "tier_a": 0},
            "4h": {"raw": 0, "tier_a": 0},
        },
        "warmup_complete": None,
        "warmup_complete_by_tf": {},
        "warmup_schema_error": None,
        "multi_tf_collision_count": 0,
        "returncode": None,
        "error_code": None,
        "artifact_reference": None,
        "message": "",
    }


def safe_artifact_reference(raw: Any, *, symbol: Any, runner_run_id: Any) -> str | None:
    symbol_s = str(symbol or "")
    run_id = str(runner_run_id or "")
    if not symbol_s or not run_id:
        return None
    if ".." in symbol_s or ".." in run_id or "/" in symbol_s or "/" in run_id:
        return None
    expected = f"coin_runs/{symbol_s}/{run_id}"
    text = str(raw or "")
    if ".." in text:
        return None
    if text.startswith("/"):
        return expected
    if text and text != expected and not text.endswith(expected):
        return expected
    return expected


def redact_public(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: redact_public(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_public(v) for v in obj]
    if isinstance(obj, str):
        if "/home/" in obj or obj.startswith("/home"):
            return "[redacted-path]"
        if obj.startswith("/") and "results/" in obj:
            return "[redacted-path]"
    return obj


def public_coin(row: dict[str, Any]) -> dict[str, Any]:
    by = row.get("warmup_complete_by_tf") or {}
    if not isinstance(by, dict):
        by = {}
    by_out = {tf: by[tf] for tf in SIGNAL_TFS if tf in by and isinstance(by.get(tf), bool)}
    return {
        "symbol": row.get("symbol"),
        "state": row.get("state"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "duration_seconds": row.get("duration_seconds"),
        "runner_run_id": row.get("runner_run_id"),
        "raw_total": row.get("raw_total"),
        "tier_a_total": row.get("tier_a_total"),
        "per_timeframe": row.get("per_timeframe"),
        "warmup_complete": row.get("warmup_complete") if isinstance(row.get("warmup_complete"), bool) else None,
        "warmup_complete_by_tf": by_out,
        "warmup_schema_error": row.get("warmup_schema_error"),
        "multi_tf_collision_count": row.get("multi_tf_collision_count"),
        "returncode": row.get("returncode"),
        "error_code": public_message(str(row.get("error_code") or "")),
        "artifact_reference": safe_artifact_reference(
            row.get("artifact_reference"),
            symbol=row.get("symbol"),
            runner_run_id=row.get("runner_run_id"),
        ),
        "message": public_message(row.get("message") or ""),
    }


def public_status(status: dict[str, Any], progress: dict[str, Any] | None = None) -> dict[str, Any]:
    coins = [public_coin(row) for row in (progress or {}).get("coins") or status.get("coins") or []]
    state = str(status.get("state") or "")
    active = state in JOB_ACTIVE_STATES
    raw_pid = status.get("worker_pid")
    last_pid = status.get("last_worker_pid")
    if not active:
        last_pid = last_pid if last_pid is not None else raw_pid
        raw_pid = None
    payload = {
        "success": True,
        "job_id": status.get("job_id"),
        "state": status.get("state"),
        "created_at": status.get("created_at"),
        "started_at": status.get("started_at"),
        "finished_at": status.get("finished_at"),
        "worker_pid": raw_pid,
        "last_worker_pid": last_pid,
        "total_coins": status.get("total_coins"),
        "completed_coins": status.get("completed_coins"),
        "successful_coins": status.get("successful_coins"),
        "failed_coins": status.get("failed_coins"),
        "current_symbol": status.get("current_symbol"),
        "current_index": status.get("current_index"),
        "progress_percent": status.get("progress_percent"),
        "raw_total": status.get("raw_total"),
        "tier_a_total": status.get("tier_a_total"),
        "error_summary": public_message(status.get("error_summary") or status.get("message") or ""),
        "message": public_message(status.get("message") or ""),
        "coins": coins,
        "combined_summary": status.get("combined_summary"),
        "active": active,
        "resumable": state in RESUME_STATES,
        "execution_dedup_applied": False,
        "outcome_evaluation_enabled": False,
        "writes_to_clickhouse": False,
        "fixed_strategy_version": STRATEGY_VERSION,
    }
    return redact_public(payload)


def load_job_public(job_id: str, environ: dict | None = None) -> dict[str, Any] | None:
    directory = job_dir_for(job_id, environ)
    status_path = directory / "status.json"
    if not status_path.exists():
        return None
    status = read_json(status_path)
    progress = None
    progress_path = directory / "progress.json"
    if progress_path.exists():
        try:
            progress = read_json(progress_path)
        except Exception:  # noqa: BLE001
            progress = None
    return public_status(status, progress)


def defaults_payload(*, now: datetime | None = None) -> dict[str, Any]:
    start = suggested_start()
    end = suggested_end_exclusive(now)
    return {
        "signal_start": window_iso(start),
        "signal_end_exclusive": window_iso(end),
        "fixed_strategy_version": STRATEGY_VERSION,
        "max_window_days": 400,
        "note": "end is exclusive UTC; last closed 1m plus one minute. Open minute excluded.",
    }


def current_or_last_status(environ: dict | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    job_id = active_job_id(environ)
    if not job_id:
        last = last_job_path(environ)
        if last.exists():
            job_id = last.read_text(encoding="utf-8").strip()
    defaults = defaults_payload(now=now)
    update_active = update_job_active_id(environ)
    extra = {
        "defaults": defaults,
        "update_job_active": bool(update_active),
        "update_job_id": update_active,
        "cross_lock": "Bidirectional exclusive start: a QUEUED/RUNNING candle update blocks Frozen POST/resume "
        "(UPDATE_JOB_BLOCKS_FROZEN_RESEARCH); a QUEUED/RUNNING Frozen job blocks candle-update POST "
        "(FROZEN_JOB_BLOCKS_CANDLE_UPDATE). Starts serialize via results/stoch_dashboard_job_start.lock. "
        "No running job is aborted.",
    }
    if not job_id:
        return {
            "success": True,
            "active": False,
            "job_id": None,
            "state": None,
            "coins": [],
            "resumable": False,
            **extra,
        }
    payload = load_job_public(job_id, environ) or {
        "success": True,
        "active": False,
        "job_id": job_id,
        "state": None,
        "coins": [],
        "resumable": False,
    }
    payload.update(extra)
    return payload


SpawnFn = Callable[[list[str], Path, Path], int]


def default_spawn_worker(argv: list[str], cwd: Path, log_path: Path) -> int:
    import subprocess as sp

    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    dash = str(DASHBOARD_ROOT)
    env["PYTHONPATH"] = dash + os.pathsep + env.get("PYTHONPATH", "")
    with log_path.open("a", encoding="utf-8") as log_fh:
        proc = sp.Popen(  # noqa: S603
            argv,
            cwd=str(cwd),
            stdout=log_fh,
            stderr=sp.STDOUT,
            shell=False,
            env=env,
        )
        return int(proc.pid)


def _write_job_files(
    directory: Path,
    *,
    job_id: str,
    cleaned: list[str],
    start: datetime,
    end: datetime,
    created: datetime,
    environ: dict | None,
    resume: bool,
) -> None:
    uni_path = universe_path(environ)
    request = {
        "job_id": job_id,
        "requested_at": iso_z(created),
        "selected_symbols": cleaned,
        "signal_start": window_iso(start),
        "signal_end_exclusive": window_iso(end),
        "fixed_strategy_version": STRATEGY_VERSION,
        "universe_source": str(uni_path),
        "universe_count": len(load_tradeable_51(uni_path)),
        "jobs_root": str(jobs_root(environ)),
    }
    manifest = {
        **request,
        "runner_code_revision": code_revision(environ),
        "frozen_source_commit": SOURCE_COMMIT,
        "frozen_module_hashes": dict(FROZEN_MODULE_HASHES),
        "side_effect_flags": dict(SIDE_EFFECT_FLAGS),
        "python_path": str(sg_python(environ)),
        "sequential": True,
        "max_parallelism": 1,
        "pool_v1_enabled": False,
        "outcome_evaluation_enabled": False,
        "writes_to_clickhouse": False,
        "publish": False,
        "live_orders": False,
        "coin_timeout_s": coin_timeout_s(environ),
        "jobs_root": str(jobs_root(environ)),
        "repo_root": str(REPO_ROOT),
        "resume": resume,
    }
    coins = [empty_coin_row(s) for s in cleaned]
    status = {
        "job_id": job_id,
        "state": "QUEUED",
        "created_at": iso_z(created),
        "started_at": None,
        "finished_at": None,
        "worker_pid": None,
        "total_coins": len(cleaned),
        "completed_coins": 0,
        "successful_coins": 0,
        "failed_coins": 0,
        "current_symbol": None,
        "current_index": 0,
        "progress_percent": 0,
        "raw_total": 0,
        "tier_a_total": 0,
        "error_summary": "",
        "message": "queued",
        "coins": coins,
        "combined_summary": None,
    }
    if not resume:
        write_json_atomic(directory / "request.json", request)
        write_json_atomic(directory / "job_manifest.json", manifest)
        write_json_atomic(
            directory / "selected_universe.json",
            {"symbols": cleaned, "universe_source": str(uni_path), "count": len(cleaned)},
        )
        (directory / "worker.log").write_text("", encoding="utf-8")
        (directory / "per_coin.jsonl").write_text("", encoding="utf-8")
    write_json_atomic(directory / "status.json", status)
    write_json_atomic(directory / "progress.json", {"coins": coins})


def _spawn_and_lock(
    job_id: str,
    directory: Path,
    *,
    environ: dict | None,
    spawn: SpawnFn | None,
) -> tuple[dict[str, Any], int]:
    dash_python = sys.executable
    if environ and environ.get("STOCH_FADE_DASH_PYTHON"):
        dash_python = str(environ["STOCH_FADE_DASH_PYTHON"])
    argv = [dash_python, str(WORKER_SCRIPT), job_id, str(directory)]
    worker_cwd = DASHBOARD_ROOT
    if environ and environ.get("STOCH_FADE_WORKER_CWD"):
        worker_cwd = Path(str(environ["STOCH_FADE_WORKER_CWD"]))
    write_json_atomic(
        lock_path(environ),
        {"job_id": job_id, "pid": None, "started_at": iso_z(_utcnow())},
    )
    spawn_fn = spawn or default_spawn_worker
    try:
        pid = spawn_fn(argv, worker_cwd, directory / "worker.log")
    except Exception as exc:  # noqa: BLE001
        status = read_json(directory / "status.json")
        status["state"] = "FAILED"
        status["finished_at"] = iso_z(_utcnow())
        status["message"] = public_message(str(exc))
        status["error_summary"] = status["message"]
        write_json_atomic(directory / "status.json", status)
        clear_lock(environ)
        heavy_gate.release(job_id, environ=environ)
        return {"success": False, "error": "SPAWN_FAILED", "job_id": job_id}, 500
    status = read_json(directory / "status.json")
    status["state"] = "RUNNING"
    status["worker_pid"] = pid
    status["last_worker_pid"] = pid
    status["started_at"] = iso_z(_utcnow())
    status["message"] = "Frozen-Signale werden berechnet"
    write_json_atomic(directory / "status.json", status)
    write_json_atomic(
        lock_path(environ),
        {"job_id": job_id, "pid": pid, "started_at": status["started_at"]},
    )
    heavy_gate.refresh_pid(job_id, pid, environ=environ)
    last_job_path(environ).write_text(job_id, encoding="utf-8")
    return {"success": True, "job_id": job_id, "state": "QUEUED", "symbols": status.get("coins")}, 200


def start_frozen_job(
    symbols: list[str],
    signal_start: str,
    signal_end_exclusive: str,
    *,
    environ: dict | None = None,
    now: datetime | None = None,
    spawn: SpawnFn | None = None,
    coverage_payload: dict[str, Any] | None = None,
    disk_free: int | None = None,
) -> tuple[dict[str, Any], int]:
    allowed = load_tradeable_51(universe_path(environ))
    cleaned, err = validate_symbols(symbols, allowed)
    if err or cleaned is None:
        return {"success": False, "error": err or "INVALID_SYMBOLS"}, 400
    try:
        start = parse_utc_minute(signal_start)
        end = parse_utc_minute(signal_end_exclusive)
        validate_window(start, end, now=now)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}, 400

    payload = coverage_payload if coverage_payload is not None else coverage_report(
        use_cache=True, environ=environ, now=now
    )
    testable, terr = filter_testable(cleaned, payload.get("coins") or [])
    if terr or testable is None:
        return {"success": False, "error": terr or "SYMBOL_NOT_TESTABLE"}, 400
    cleaned = testable

    root = jobs_root(environ)
    if disk_free is not None:
        from .config import DISK_RESERVE_BYTES, MIN_FREE_BYTES_51, PER_COIN_DISK_BYTES

        need = len(cleaned) * PER_COIN_DISK_BYTES + DISK_RESERVE_BYTES
        if len(cleaned) >= 51:
            need = max(need, MIN_FREE_BYTES_51)
        if disk_free < need:
            return {"success": False, "error": "INSUFFICIENT_DISK"}, 400
    else:
        disk_err = assert_disk(root, len(cleaned))
        if disk_err:
            return {"success": False, "error": disk_err}, 400

    with start_gate(environ):
        if update_job_active_id(environ):
            return {
                "success": False,
                "error": "UPDATE_JOB_BLOCKS_FROZEN_RESEARCH",
                "job_id": update_job_active_id(environ),
            }, 409
        active = active_job_id(environ)
        if active:
            return {"success": False, "error": "FROZEN_JOB_ALREADY_RUNNING", "job_id": active}, 409

        job_id = uuid.uuid4().hex
        acquired, gate_err, existing = heavy_gate.try_acquire(
            heavy_gate.OWNER_FROZEN_RESEARCH, job_id, environ=environ
        )
        if not acquired:
            return {
                "success": False,
                "error": gate_err or "HEAVY_JOB_RESOURCE_BUSY",
                "job_id": (existing or {}).get("job_id"),
            }, 409
        created = now or _utcnow()
        directory = job_dir_for(job_id, environ)
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except Exception:
            heavy_gate.release(job_id, environ=environ)
            raise
        _write_job_files(
            directory,
            job_id=job_id,
            cleaned=cleaned,
            start=start,
            end=end,
            created=created,
            environ=environ,
            resume=False,
        )
        payload, code = _spawn_and_lock(job_id, directory, environ=environ, spawn=spawn)
        if code != 200:
            heavy_gate.release(job_id, environ=environ)
    if code == 200:
        payload["symbols"] = cleaned
        payload["signal_start"] = window_iso(start)
        payload["signal_end_exclusive"] = window_iso(end)
        payload["fixed_strategy_version"] = STRATEGY_VERSION
    return payload, code


def resume_frozen_job(
    job_id: str,
    *,
    environ: dict | None = None,
    spawn: SpawnFn | None = None,
) -> tuple[dict[str, Any], int]:
    with start_gate(environ):
        if update_job_active_id(environ):
            return {"success": False, "error": "UPDATE_JOB_BLOCKS_FROZEN_RESEARCH"}, 409
        active = active_job_id(environ)
        if active:
            return {"success": False, "error": "FROZEN_JOB_ALREADY_RUNNING", "job_id": active}, 409
        directory = job_dir_for(job_id, environ)
        req_path = directory / "request.json"
        status_path = directory / "status.json"
        if not req_path.exists() or not status_path.exists():
            return {"success": False, "error": "JOB_NOT_FOUND"}, 404
        request = read_json(req_path)
        status = read_json(status_path)
        if str(status.get("state")) in JOB_ACTIVE_STATES:
            return {"success": False, "error": "JOB_STILL_ACTIVE"}, 409
        if str(status.get("state")) not in RESUME_STATES:
            return {"success": False, "error": "RESUME_NOT_ALLOWED"}, 409
        if str(request.get("job_id")) != job_id:
            return {"success": False, "error": "REQUEST_MISMATCH"}, 400
        acquired, gate_err, existing = heavy_gate.try_acquire(
            heavy_gate.OWNER_FROZEN_RESEARCH, job_id, environ=environ
        )
        if not acquired:
            return {
                "success": False,
                "error": gate_err or "HEAVY_JOB_RESOURCE_BUSY",
                "job_id": (existing or {}).get("job_id"),
            }, 409
        original_request = dict(request)
        cleaned = list(request["selected_symbols"])
        created = _utcnow()
        prev_coins = {c["symbol"]: c for c in (status.get("coins") or [])}
        coins = []
        for symbol in cleaned:
            row = dict(prev_coins.get(symbol) or empty_coin_row(symbol))
            if row.get("state") in ("COMPLETED", "SKIPPED_RESUME_COMPLETE"):
                coins.append(row)
                continue
            pending = empty_coin_row(symbol)
            pending["message"] = "retry pending"
            coins.append(pending)
        new_status = {
            "job_id": job_id,
            "state": "QUEUED",
            "created_at": status.get("created_at") or iso_z(created),
            "started_at": None,
            "finished_at": None,
            "worker_pid": None,
            "total_coins": len(cleaned),
            "completed_coins": sum(
                1 for c in coins if c.get("state") in ("COMPLETED", "SKIPPED_RESUME_COMPLETE")
            ),
            "successful_coins": sum(
                1 for c in coins if c.get("state") in ("COMPLETED", "SKIPPED_RESUME_COMPLETE")
            ),
            "failed_coins": 0,
            "current_symbol": None,
            "current_index": 0,
            "progress_percent": 0,
            "raw_total": sum(int(c.get("raw_total") or 0) for c in coins),
            "tier_a_total": sum(int(c.get("tier_a_total") or 0) for c in coins),
            "error_summary": "",
            "message": "resume queued",
            "coins": coins,
            "combined_summary": None,
            "resume": True,
        }
        write_json_atomic(directory / "status.json", new_status)
        write_json_atomic(directory / "progress.json", {"coins": coins})
        write_json_atomic(directory / "request.json", original_request)
        payload, code = _spawn_and_lock(job_id, directory, environ=environ, spawn=spawn)
        if code != 200:
            heavy_gate.release(job_id, environ=environ)
    if code == 200:
        payload["resumed"] = True
        payload["symbols"] = cleaned
    return payload, code


def handle_create_post(
    *,
    body: dict[str, Any],
    origin: str | None,
    referer: str | None,
    content_type: str | None,
    environ: dict | None = None,
    now: datetime | None = None,
    spawn: SpawnFn | None = None,
    coverage_payload: dict[str, Any] | None = None,
    disk_free: int | None = None,
) -> tuple[dict[str, Any], int]:
    guard = update_post_guard(origin=origin, referer=referer, content_type=content_type)
    if guard:
        return {"success": False, "error": guard}, 403
    if not isinstance(body, dict):
        return {"success": False, "error": "UNKNOWN_FIELDS"}, 400
    try:
        parsed = FrozenFadeJobBody(**body)
    except Exception:  # noqa: BLE001
        return {"success": False, "error": "UNKNOWN_FIELDS"}, 400
    return start_frozen_job(
        list(parsed.symbols),
        parsed.signal_start,
        parsed.signal_end_exclusive,
        environ=environ,
        now=now,
        spawn=spawn,
        coverage_payload=coverage_payload,
        disk_free=disk_free,
    )


def handle_resume_post(
    *,
    job_id: str,
    origin: str | None,
    referer: str | None,
    content_type: str | None,
    body: dict[str, Any] | None = None,
    environ: dict | None = None,
    spawn: SpawnFn | None = None,
) -> tuple[dict[str, Any], int]:
    guard = update_post_guard(origin=origin, referer=referer, content_type=content_type)
    if guard:
        return {"success": False, "error": guard}, 403
    if body:
        return {"success": False, "error": "UNKNOWN_FIELDS"}, 400
    return resume_frozen_job(job_id, environ=environ, spawn=spawn)
