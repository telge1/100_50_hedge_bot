"""Single-flight 51-universe candle update jobs. No signal pipeline, no cleanup-first."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import sys

from .config import (
    DASHBOARD_ROOT,
    REQUESTED_FROM,
    backfill_script,
    jobs_root,
    sg_python,
    signal_generator_root,
    universe_path,
)
from .coverage import bump_coverage_generation, clear_coverage_cache, coverage_report, iso_z
from .jsonio import read_json, write_json_atomic
from .origin import update_post_guard
from .universe import load_tradeable_51
from .update_plan import (
    argv_for_call,
    last_closed_end_exclusive,
    plan_symbol_update,
    validate_update_symbols,
)

JOB_STATES = ("QUEUED", "RUNNING", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED")
COIN_STATES = ("QUEUED", "UPDATING", "ALREADY_CURRENT", "COMPLETED", "FAILED")
SECRET_MARKERS = ("PASSWORD", "SECRET", "API_KEY", "TOKEN", "BYBIT_KEY", "CLICKHOUSE_PASSWORD")
FORBIDDEN_SCRIPTS = ("run_wave_fade_shadow_pipeline.py", "live_universe.json")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def public_message(text: str, limit: int = 180) -> str:
    raw = " ".join(str(text or "").split())
    upper = raw.upper()
    for marker in SECRET_MARKERS:
        if marker in upper:
            return "Aktualisierung fehlgeschlagen"
    if any(p in raw for p in ("/.env", "BEGIN RSA", "AWS_")):
        return "Aktualisierung fehlgeschlagen"
    return raw[:limit]


def write_temp_universe(job_dir: Path, symbols: list[str], source_path: Path) -> Path:
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("universe source must be an object")
    wanted = set(symbols)
    details = [d for d in (raw.get("details") or []) if isinstance(d, dict) and d.get("symbol") in wanted]
    payload = {
        "generated_at": raw.get("generated_at") or iso_z(_utcnow()),
        "source": "stoch_universe_51_update_job",
        "selection_method": raw.get("selection_method") or "allowlist_subset",
        "target_size": len(symbols),
        "symbols": list(symbols),
        "details": details,
    }
    path = job_dir / "universe_selected.json"
    write_json_atomic(path, payload)
    return path


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
    return "update_worker.py" in cmd and job_id in cmd


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
        if str(status.get("state")) in ("QUEUED", "RUNNING"):
            status["state"] = "FAILED"
            status["finished_at"] = iso_z(_utcnow())
            status["message"] = public_message("verwaister Update-Prozess beendet")
            write_json_atomic(status_path, status)
            bump_coverage_generation(environ)
    clear_lock(environ)
    return None


def active_job_id(environ: dict | None = None) -> str | None:
    lock = reconcile_lock(environ)
    if lock:
        return str(lock.get("job_id") or "") or None
    return None


def public_status(status: dict[str, Any], progress: dict[str, Any] | None = None) -> dict[str, Any]:
    coins = []
    for row in (progress or {}).get("coins") or status.get("coins") or []:
        coins.append(
            {
                "symbol": row.get("symbol"),
                "state": row.get("state"),
                "message": public_message(row.get("message") or ""),
            }
        )
    return {
        "success": True,
        "job_id": status.get("job_id"),
        "state": status.get("state"),
        "started_at": status.get("started_at"),
        "finished_at": status.get("finished_at"),
        "return_code": status.get("return_code"),
        "current_symbol": status.get("current_symbol"),
        "completed_symbols": status.get("completed_symbols"),
        "total_symbols": status.get("total_symbols"),
        "success_count": status.get("success_count"),
        "failed_count": status.get("failed_count"),
        "already_current_count": status.get("already_current_count"),
        "message": public_message(status.get("message") or ""),
        "coins": coins,
        "active": str(status.get("state")) in ("QUEUED", "RUNNING"),
    }


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
    payload = public_status(status, progress)
    if str(status.get("state")) in ("COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"):
        clear_coverage_cache()
    return payload


def current_or_last_status(environ: dict | None = None) -> dict[str, Any]:
    job_id = active_job_id(environ)
    if not job_id:
        last = last_job_path(environ)
        if last.exists():
            job_id = last.read_text(encoding="utf-8").strip()
    if not job_id:
        return {"success": True, "active": False, "job_id": None, "state": None, "coins": []}
    payload = load_job_public(job_id, environ) or {
        "success": True,
        "active": False,
        "job_id": job_id,
        "state": None,
        "coins": [],
    }
    return payload


SpawnFn = Callable[[list[str], Path, Path], int]


def default_spawn_worker(argv: list[str], cwd: Path, log_path: Path) -> int:
    import subprocess

    if any(tok in " ".join(argv) for tok in FORBIDDEN_SCRIPTS):
        raise RuntimeError("forbidden script")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    dash = str(DASHBOARD_ROOT)
    env["PYTHONPATH"] = dash + os.pathsep + env.get("PYTHONPATH", "")
    with log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(  # noqa: S603 — argv list, shell=False
            argv,
            cwd=str(cwd),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            shell=False,
            env=env,
        )
        return int(proc.pid)


def start_update_job(
    symbols: list[str],
    *,
    environ: dict | None = None,
    now: datetime | None = None,
    spawn: SpawnFn | None = None,
    coverage_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    allowed = load_tradeable_51(universe_path(environ))
    cleaned, err = validate_update_symbols(symbols, allowed)
    if err or cleaned is None:
        code = 400
        return {"success": False, "error": err or "INVALID_SYMBOLS"}, code
    active = active_job_id(environ)
    if active:
        return {
            "success": False,
            "error": "UPDATE_JOB_ALREADY_RUNNING",
            "job_id": active,
        }, 409

    payload = coverage_payload if coverage_payload is not None else coverage_report(
        use_cache=True, environ=environ, now=now
    )
    by_symbol = {c["symbol"]: c for c in payload.get("coins") or []}
    plans = []
    for symbol in cleaned:
        coin = by_symbol.get(symbol)
        if coin is None:
            return {"success": False, "error": "UNKNOWN_SYMBOL", "symbol": symbol}, 400
        plans.append(plan_symbol_update(coin, now=now))

    job_id = uuid.uuid4().hex
    created = now or _utcnow()
    end_exclusive = last_closed_end_exclusive(created)
    root = jobs_root(environ)
    root.mkdir(parents=True, exist_ok=True)
    directory = job_dir_for(job_id, environ)
    directory.mkdir(parents=True, exist_ok=False)
    universe_file = write_temp_universe(directory, cleaned, universe_path(environ))

    request = {
        "job_id": job_id,
        "created_at": iso_z(created),
        "symbols": cleaned,
        "requested_from": iso_z(REQUESTED_FROM),
        "update_to": iso_z(end_exclusive),
        "repair_missing": True,
        "plans": plans,
        "universe_file": universe_file.name,
        "sg_root": str(signal_generator_root(environ)),
        "sg_python": str(sg_python(environ)),
        "backfill_script": str(backfill_script(environ)),
        "jobs_root": str(jobs_root(environ)),
    }
    coins_progress = []
    already = 0
    for plan in plans:
        state = "ALREADY_CURRENT" if plan["action"] == "ALREADY_CURRENT" else "QUEUED"
        if state == "ALREADY_CURRENT":
            already += 1
        coins_progress.append(
            {
                "symbol": plan["symbol"],
                "state": state,
                "message": plan.get("message") or "",
            }
        )
    status = {
        "job_id": job_id,
        "state": "QUEUED",
        "pid": None,
        "started_at": None,
        "finished_at": None,
        "return_code": None,
        "current_symbol": None,
        "completed_symbols": already,
        "total_symbols": len(cleaned),
        "success_count": already,
        "failed_count": 0,
        "already_current_count": already,
        "message": "queued",
        "coins": coins_progress,
    }
    write_json_atomic(directory / "request.json", request)
    write_json_atomic(directory / "status.json", status)
    write_json_atomic(directory / "progress.json", {"coins": coins_progress})
    (directory / "update.log").write_text("", encoding="utf-8")
    last_job_path(environ).write_text(job_id, encoding="utf-8")

    worker = Path(__file__).resolve().parent / "update_worker.py"
    dash_python = sys.executable
    if environ and environ.get("STOCH_UNIVERSE_51_DASH_PYTHON"):
        dash_python = str(environ["STOCH_UNIVERSE_51_DASH_PYTHON"])
    argv = [dash_python, str(worker), job_id, str(directory)]
    worker_cwd = DASHBOARD_ROOT
    if environ and environ.get("STOCH_UNIVERSE_51_WORKER_CWD"):
        worker_cwd = Path(str(environ["STOCH_UNIVERSE_51_WORKER_CWD"]))

    write_json_atomic(
        lock_path(environ),
        {"job_id": job_id, "pid": None, "started_at": iso_z(created)},
    )
    spawn_fn = spawn or default_spawn_worker
    try:
        pid = spawn_fn(argv, worker_cwd, directory / "update.log")
    except Exception as exc:  # noqa: BLE001
        status["state"] = "FAILED"
        status["finished_at"] = iso_z(_utcnow())
        status["message"] = public_message(str(exc))
        write_json_atomic(directory / "status.json", status)
        clear_lock(environ)
        bump_coverage_generation(environ)
        return {"success": False, "error": "SPAWN_FAILED", "job_id": job_id}, 500

    status["state"] = "RUNNING"
    status["pid"] = pid
    status["started_at"] = iso_z(_utcnow())
    status["message"] = "Datenaktualisierung läuft"
    write_json_atomic(directory / "status.json", status)
    write_json_atomic(
        lock_path(environ),
        {"job_id": job_id, "pid": pid, "started_at": status["started_at"]},
    )
    return {
        "success": True,
        "job_id": job_id,
        "state": "QUEUED",
        "symbols": cleaned,
    }, 200


def handle_update_post(
    *,
    symbols: list[str],
    origin: str | None,
    referer: str | None,
    content_type: str | None,
    extra_fields: dict[str, Any] | None = None,
    environ: dict | None = None,
    now: datetime | None = None,
    spawn: SpawnFn | None = None,
    coverage_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    if extra_fields:
        return {"success": False, "error": "UNKNOWN_FIELDS"}, 400
    guard = update_post_guard(origin=origin, referer=referer, content_type=content_type)
    if guard:
        return {"success": False, "error": guard}, 403
    return start_update_job(
        symbols,
        environ=environ,
        now=now,
        spawn=spawn,
        coverage_payload=coverage_payload,
    )


def backfill_argv_from_plan(
    plan_call: dict[str, Any],
    *,
    python: str,
    script: str,
    universe_file: str,
    symbol: str,
    out_dir: str,
    checkpoint: str,
) -> list[str]:
    argv = argv_for_call(
        python=python,
        script=script,
        universe_file=universe_file,
        symbol=symbol,
        start=plan_call["start"],
        end=plan_call["end"],
        out_dir=out_dir,
        checkpoint=checkpoint,
        repair_missing=bool(plan_call.get("repair_missing")),
        resume=bool(plan_call.get("resume")),
    )
    if "shell=True" in argv:
        raise RuntimeError("shell forbidden")
    return argv
