"""Create/resume Frozen-signal NO_BE50 evaluation jobs. FastAPI must not import the outcome engine."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from stoch_universe_51.jsonio import read_json, write_json_atomic
from stoch_universe_51.origin import update_post_guard
from stoch_universe_51.update_jobs import active_job_id as update_job_active_id

import stoch_heavy_job_gate as heavy_gate
from worker_env import inject_worker_env
from stoch_fade_research_jobs.complete import coin_run_is_complete
from stoch_fade_research_jobs.config import STRATEGY_VERSION as FROZEN_STRATEGY
from stoch_fade_research_jobs.cross_lock import start_gate
from stoch_fade_research_jobs.feed import SELECTABLE_STATES, parse_job_id, resolve_job_dir
from stoch_fade_research_jobs.jobs import (
    active_job_id as fade_active_id,
    public_message,
    redact_public,
    safe_artifact_reference,
)

from .artifacts import SIDE_EFFECT_FLAGS, apply_source_counts, empty_coin_row
from .config import (
    CAUSAL_MANIFEST_HASH,
    CONFIRMATION_POLICY,
    CONFIRMATION_SOURCE,
    COIN_TERM_GRACE_S,
    DISK_RESERVE_BYTES,
    EXIT_POLICY,
    INTRABAR_POLICY,
    OUTCOME_ENGINE,
    PER_COIN_DISK_BYTES,
    REPO_ROOT,
    SIGNAL_SCOPE,
    SIGNAL_SOURCE_COMMIT,
    SIGNAL_STRATEGY_VERSION,
    SOURCE,
    STRATEGY_VERSION,
    WORKER_SCRIPT,
    coin_timeout_s,
    evaluations_root,
    sg_python,
)
from .schema import FrozenFadeEvalBody

SECRET_MARKERS = ("PASSWORD", "SECRET", "API_KEY", "TOKEN", "BYBIT_KEY", "CLICKHOUSE_PASSWORD")
JOB_ACTIVE_STATES = ("QUEUED", "RUNNING")
RESUME_STATES = ("FAILED", "INTERRUPTED", "COMPLETED_WITH_ERRORS")
WORKER_NAME = "stoch_fade_research_evaluations/worker.py"
SpawnFn = Callable[..., int]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(ts: datetime | None = None) -> str:
    now = ts or _utcnow()
    return now.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def eval_dir_for(evaluation_id: str, environ: dict | None = None) -> Path:
    return evaluations_root(environ) / evaluation_id


def lock_path(environ: dict | None = None) -> Path:
    return evaluations_root(environ) / "ACTIVE.lock"


def last_eval_path(environ: dict | None = None) -> Path:
    return evaluations_root(environ) / "last_evaluation_id.txt"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def source_artifact_hashes(job_dir: Path, status: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in ("request.json", "status.json", "combined_summary.json"):
        path = job_dir / name
        if path.is_file():
            out[name] = _sha256_file(path)
    for coin in status.get("coins") or []:
        if not isinstance(coin, dict) or str(coin.get("state")) != "COMPLETED":
            continue
        symbol = str(coin.get("symbol") or "")
        run_id = str(coin.get("runner_run_id") or "")
        ref = safe_artifact_reference(coin.get("artifact_reference"), symbol=symbol, runner_run_id=run_id)
        if not ref:
            continue
        sig = job_dir / ref / "signals.jsonl"
        if sig.is_file():
            out[f"{ref}/signals.jsonl"] = _sha256_file(sig)
    return out


def _proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def worker_is_live(pid: int | None, evaluation_id: str) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    cmd = _proc_cmdline(int(pid)).replace("\\", "/")
    return WORKER_NAME in cmd and evaluation_id in cmd


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
        clear_lock(environ)
        return None
    evaluation_id = str(lock.get("evaluation_id") or lock.get("job_id") or "")
    pid = lock.get("pid")
    if worker_is_live(pid if isinstance(pid, int) else None, evaluation_id):
        return lock
    clear_lock(environ)
    return None


def active_evaluation_id(environ: dict | None = None) -> str | None:
    lock = reconcile_lock(environ)
    if not lock:
        return None
    return str(lock.get("evaluation_id") or lock.get("job_id") or "") or None


def public_eval(status: dict[str, Any], progress: dict[str, Any] | None = None) -> dict[str, Any]:
    coins = progress.get("coins") if progress else status.get("coins") or []
    state = str(status.get("state") or "")
    active = state in JOB_ACTIVE_STATES
    raw_pid = status.get("worker_pid")
    last_pid = status.get("last_worker_pid")
    if not active:
        last_pid = last_pid if last_pid is not None else raw_pid
        raw_pid = None
    payload = {
        "success": True,
        "source": SOURCE,
        "evaluation_id": status.get("evaluation_id"),
        "source_job_id": status.get("source_job_id"),
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
        "progress_percent": status.get("progress_percent"),
        "tier_a_total": status.get("tier_a_total"),
        "wins": status.get("wins"),
        "losses": status.get("losses"),
        "open": status.get("open"),
        "message": public_message(status.get("message") or ""),
        "coins": coins,
        "combined_summary": status.get("combined_summary"),
        "active": active,
        "resumable": state in RESUME_STATES,
        "exit_policy": EXIT_POLICY,
        "signal_strategy_version": SIGNAL_STRATEGY_VERSION,
        "signal_source_commit": SIGNAL_SOURCE_COMMIT,
        "outcome_engine": OUTCOME_ENGINE,
        "intrabar_policy": INTRABAR_POLICY,
        "signal_scope": SIGNAL_SCOPE,
        "execution_dedup_applied": False,
        "fixed_strategy_version": STRATEGY_VERSION,
        "side_effect_flags": dict(SIDE_EFFECT_FLAGS),
        "writes_to_clickhouse": False,
        "outcome_evaluation_enabled": True,
    }
    return redact_public(payload)


def load_eval_public(evaluation_id: str, environ: dict | None = None) -> dict[str, Any] | None:
    parsed = parse_job_id(evaluation_id)
    if parsed is None:
        return None
    directory = eval_dir_for(parsed, environ)
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
    return public_eval(status, progress)


def current_or_last_status(environ: dict | None = None) -> dict[str, Any]:
    heavy_gate.reconcile_gate(environ)
    active = active_evaluation_id(environ)
    if active:
        payload = load_eval_public(active, environ)
        if payload:
            payload["active"] = True
            return payload
    last = last_eval_path(environ)
    if last.is_file():
        eid = last.read_text(encoding="utf-8").strip()
        payload = load_eval_public(eid, environ)
        if payload:
            payload["active"] = False
            return payload
    return {"success": True, "active": False, "evaluation_id": None, "state": None, "coins": []}


def _validate_source_job(source_job_id: str, environ: dict | None = None) -> tuple[dict[str, Any] | None, str | None]:
    parsed = parse_job_id(source_job_id)
    if parsed is None:
        return None, "JOB_ID_INVALID"
    job_dir = resolve_job_dir(parsed, environ)
    if job_dir is None:
        return None, "SOURCE_JOB_NOT_FOUND"
    request = read_json(job_dir / "request.json")
    status = read_json(job_dir / "status.json")
    if str(status.get("state") or "") not in SELECTABLE_STATES:
        return None, "SOURCE_JOB_NOT_SELECTABLE"
    if str(request.get("fixed_strategy_version") or FROZEN_STRATEGY) != FROZEN_STRATEGY:
        return None, "FROZEN_IDENTITY_MISMATCH"
    if str(request.get("confirmation_policy") or "") != CONFIRMATION_POLICY:
        return None, "FROZEN_IDENTITY_MISMATCH"
    if str(request.get("causal_manifest_hash") or "") != CAUSAL_MANIFEST_HASH:
        return None, "FROZEN_IDENTITY_MISMATCH"
    symbols = [str(s) for s in (request.get("selected_symbols") or []) if s]
    coins = []
    tier_a = 0
    for coin in status.get("coins") or []:
        if not isinstance(coin, dict) or str(coin.get("state")) != "COMPLETED":
            continue
        symbol = str(coin.get("symbol") or "")
        run_id = str(coin.get("runner_run_id") or "")
        ref = safe_artifact_reference(coin.get("artifact_reference"), symbol=symbol, runner_run_id=run_id)
        if not ref:
            continue
        run_dir = job_dir / ref
        if not coin_run_is_complete(
            run_dir,
            symbol=symbol,
            signal_start=str(request.get("signal_start") or ""),
            signal_end_exclusive=str(request.get("signal_end_exclusive") or ""),
        ):
            continue
        tia = int(coin.get("tier_a_total") or 0)
        if tia <= 0:
            continue
        coins.append(
            {
                "symbol": symbol,
                "runner_run_id": run_id,
                "artifact_reference": ref,
                "tier_a_total": tia,
                "raw_total": int(coin.get("raw_total") or 0),
                "signals_path": f"{ref}/signals.jsonl",
            }
        )
        tier_a += tia
    if not coins or tier_a <= 0:
        return None, "NO_TIER_A_SIGNALS"
    return {
        "source_job_id": parsed,
        "job_dir_name": job_dir.name,
        "request": request,
        "status": status,
        "coins": coins,
        "tier_a_total": tier_a,
        "hashes": source_artifact_hashes(job_dir, status),
        "signal_start": request.get("signal_start"),
        "signal_end_exclusive": request.get("signal_end_exclusive"),
        "selected_symbols": [c["symbol"] for c in coins],
    }, None


def _write_eval_files(
    directory: Path,
    *,
    evaluation_id: str,
    source: dict[str, Any],
    created: datetime,
    resume: bool,
    outcome_data_end: str | None = None,
    prev_status: dict[str, Any] | None = None,
) -> None:
    resolved_outcome_data_end = outcome_data_end or str(source.get("signal_end_exclusive") or "")
    coins = [empty_coin_row(c["symbol"], c) for c in source["coins"]]
    if resume and prev_status:
        prev = {c["symbol"]: c for c in (prev_status.get("coins") or [])}
        rebuilt = []
        for src in source["coins"]:
            old = prev.get(src["symbol"]) or {}
            if old.get("state") in ("COMPLETED", "SKIPPED_RESUME_COMPLETE"):
                rebuilt.append(apply_source_counts(dict(old), src))
            else:
                rebuilt.append(empty_coin_row(src["symbol"], src))
        coins = rebuilt
    request = {
        "evaluation_id": evaluation_id,
        "source_job_id": source["source_job_id"],
        "fixed_strategy_version": STRATEGY_VERSION,
        "signal_strategy_version": SIGNAL_STRATEGY_VERSION,
        "signal_source_commit": SIGNAL_SOURCE_COMMIT,
        "confirmation_policy": CONFIRMATION_POLICY,
        "confirmation_source": CONFIRMATION_SOURCE,
        "causal_manifest_hash": CAUSAL_MANIFEST_HASH,
        "exit_policy": EXIT_POLICY,
        "outcome_engine": OUTCOME_ENGINE,
        "intrabar_policy": INTRABAR_POLICY,
        "signal_scope": SIGNAL_SCOPE,
        "execution_dedup_applied": False,
        "selected_symbols": source["selected_symbols"],
        "requested_at": iso_z(created),
        "outcome_data_end": resolved_outcome_data_end,
    }
    manifest = {
        "evaluation_id": evaluation_id,
        "source_job_id": source["source_job_id"],
        "source_job_manifest_hash": source["hashes"].get("request.json"),
        "source_signal_artifact_hashes": source["hashes"],
        "strategy_version": STRATEGY_VERSION,
        "signal_strategy_version": SIGNAL_STRATEGY_VERSION,
        "signal_source_commit": SIGNAL_SOURCE_COMMIT,
        "confirmation_policy": CONFIRMATION_POLICY,
        "confirmation_source": CONFIRMATION_SOURCE,
        "causal_manifest_hash": CAUSAL_MANIFEST_HASH,
        "exit_policy": EXIT_POLICY,
        "outcome_engine": OUTCOME_ENGINE,
        "intrabar_policy": INTRABAR_POLICY,
        "signal_scope": SIGNAL_SCOPE,
        "execution_dedup_applied": False,
        "signals_evaluated_independently": True,
        "pnl_basis": "gross",
        "fee_policy": "cards_use_gross_only",
        "candle_source": "signal_generator.candles_1m FINAL",
        "evaluation_data_start": source["signal_start"],
        "evaluation_data_end": resolved_outcome_data_end,
        "side_effect_flags": dict(SIDE_EFFECT_FLAGS),
        "created_at": iso_z(created),
    }
    status = {
        "evaluation_id": evaluation_id,
        "source_job_id": source["source_job_id"],
        "state": "QUEUED",
        "created_at": iso_z(created) if not resume else (prev_status or {}).get("created_at"),
        "started_at": iso_z(created),
        "finished_at": None,
        "worker_pid": None,
        "total_coins": len(coins),
        "completed_coins": sum(1 for c in coins if c.get("state") in ("COMPLETED", "SKIPPED_RESUME_COMPLETE")),
        "successful_coins": 0,
        "failed_coins": 0,
        "current_symbol": None,
        "progress_percent": 0,
        "tier_a_total": source["tier_a_total"],
        "wins": 0,
        "losses": 0,
        "open": 0,
        "message": "QUEUED",
        "coins": coins,
        "combined_summary": None,
    }
    write_json_atomic(directory / "request.json", request)
    write_json_atomic(directory / "evaluation_manifest.json", manifest)
    write_json_atomic(directory / "status.json", status)
    write_json_atomic(directory / "progress.json", {"coins": coins})
    write_json_atomic(directory / "source_index.json", {"coins": source["coins"], "hashes": source["hashes"]})
    snap = {
        "kind": "snapshot_before" if not resume else "snapshot_resume",
        "at": iso_z(created),
        "source_hashes": source["hashes"],
    }
    write_json_atomic(directory / ("snapshot_before.json" if not resume else "snapshot_resume.json"), snap)


def _spawn(evaluation_id: str, directory: Path, environ: dict | None = None, spawn: SpawnFn | None = None) -> tuple[dict[str, Any], int]:
    log_path = directory / "worker.log"
    dash_python = sys.executable
    argv = [dash_python, str(WORKER_SCRIPT), evaluation_id, str(directory)]
    if spawn is not None:
        pid = spawn(argv, str(REPO_ROOT), str(log_path))
    else:
        env = dict(os.environ)
        env, _meta = inject_worker_env(env)
        env["STOCH_FADE_SG_PYTHON"] = str(sg_python(env))
        log_path.touch()
        with log_path.open("ab") as log:
            proc = subprocess.Popen(
                argv,
                cwd=str(REPO_ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
        pid = int(proc.pid)
    if not pid:
        heavy_gate.release(evaluation_id, environ=environ)
        return {"success": False, "error": "SPAWN_FAILED", "evaluation_id": evaluation_id}, 500
    write_json_atomic(lock_path(environ), {"evaluation_id": evaluation_id, "job_id": evaluation_id, "pid": pid})
    status = read_json(directory / "status.json")
    status["worker_pid"] = pid
    status["last_worker_pid"] = pid
    status["state"] = "QUEUED"
    write_json_atomic(directory / "status.json", status)
    heavy_gate.refresh_pid(evaluation_id, pid, environ=environ)
    last_eval_path(environ).parent.mkdir(parents=True, exist_ok=True)
    last_eval_path(environ).write_text(evaluation_id, encoding="utf-8")
    return {"success": True, "evaluation_id": evaluation_id, "state": "QUEUED"}, 200


def start_evaluation(
    source_job_id: str,
    *,
    environ: dict | None = None,
    now: datetime | None = None,
    spawn: SpawnFn | None = None,
    disk_free: int | None = None,
    outcome_data_end: str | None = None,
) -> tuple[dict[str, Any], int]:
    source, err = _validate_source_job(source_job_id, environ)
    if err or source is None:
        return {"success": False, "error": err or "SOURCE_JOB_INVALID"}, 400
    root = evaluations_root(environ)
    need = len(source["coins"]) * PER_COIN_DISK_BYTES + DISK_RESERVE_BYTES
    if disk_free is not None and disk_free < need:
        return {"success": False, "error": "INSUFFICIENT_DISK"}, 400
    with start_gate(environ):
        if update_job_active_id(environ):
            return {"success": False, "error": "UPDATE_JOB_BLOCKS_OUTCOME_EVALUATION"}, 409
        if fade_active_id(environ):
            return {"success": False, "error": "FROZEN_JOB_BLOCKS_OUTCOME_EVALUATION"}, 409
        active = active_evaluation_id(environ)
        if active:
            return {"success": False, "error": "OUTCOME_EVAL_ALREADY_RUNNING", "evaluation_id": active}, 409
        evaluation_id = uuid.uuid4().hex
        acquired, gate_err, existing = heavy_gate.try_acquire(
            heavy_gate.OWNER_FROZEN_OUTCOME_EVALUATION, evaluation_id, environ=environ
        )
        if not acquired:
            return {
                "success": False,
                "error": gate_err or "HEAVY_JOB_RESOURCE_BUSY",
                "job_id": (existing or {}).get("job_id"),
            }, 409
        created = now or _utcnow()
        directory = eval_dir_for(evaluation_id, environ)
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except Exception:
            heavy_gate.release(evaluation_id, environ=environ)
            raise
        _write_eval_files(
            directory,
            evaluation_id=evaluation_id,
            source=source,
            created=created,
            resume=False,
            outcome_data_end=outcome_data_end,
        )
        payload, code = _spawn(evaluation_id, directory, environ=environ, spawn=spawn)
        if code != 200:
            heavy_gate.release(evaluation_id, environ=environ)
        else:
            payload["source_job_id"] = source["source_job_id"]
            payload["exit_policy"] = EXIT_POLICY
            payload["tier_a_total"] = source["tier_a_total"]
        return payload, code


def resume_evaluation(
    evaluation_id: str,
    *,
    environ: dict | None = None,
    spawn: SpawnFn | None = None,
) -> tuple[dict[str, Any], int]:
    parsed = parse_job_id(evaluation_id)
    if parsed is None:
        return {"success": False, "error": "JOB_ID_INVALID"}, 400
    with start_gate(environ):
        if update_job_active_id(environ):
            return {"success": False, "error": "UPDATE_JOB_BLOCKS_OUTCOME_EVALUATION"}, 409
        if fade_active_id(environ):
            return {"success": False, "error": "FROZEN_JOB_BLOCKS_OUTCOME_EVALUATION"}, 409
        active = active_evaluation_id(environ)
        if active:
            return {"success": False, "error": "OUTCOME_EVAL_ALREADY_RUNNING"}, 409
        directory = eval_dir_for(parsed, environ)
        if not (directory / "request.json").exists():
            return {"success": False, "error": "JOB_NOT_FOUND"}, 404
        request = read_json(directory / "request.json")
        status = read_json(directory / "status.json")
        if str(request.get("fixed_strategy_version") or "") != STRATEGY_VERSION:
            return {"success": False, "error": "LEGACY_WAVE_END_NON_CAUSAL"}, 409
        if str(request.get("confirmation_policy") or "") != CONFIRMATION_POLICY:
            return {"success": False, "error": "LEGACY_WAVE_END_NON_CAUSAL"}, 409
        if str(request.get("causal_manifest_hash") or "") != CAUSAL_MANIFEST_HASH:
            return {"success": False, "error": "LEGACY_WAVE_END_NON_CAUSAL"}, 409
        if str(request.get("exit_policy") or "") != EXIT_POLICY:
            return {"success": False, "error": "LEGACY_BE50_EVALUATION_NOT_RESUMABLE"}, 409
        if str(status.get("state")) in JOB_ACTIVE_STATES:
            return {"success": False, "error": "JOB_STILL_ACTIVE"}, 409
        if str(status.get("state")) not in RESUME_STATES:
            return {"success": False, "error": "RESUME_NOT_ALLOWED"}, 409
        source, err = _validate_source_job(str(request.get("source_job_id") or ""), environ)
        if err or source is None:
            return {"success": False, "error": err or "SOURCE_JOB_INVALID"}, 400
        stored = read_json(directory / "evaluation_manifest.json")
        if stored.get("source_signal_artifact_hashes") != source["hashes"]:
            return {"success": False, "error": "SOURCE_HASH_MISMATCH"}, 409
        acquired, gate_err, existing = heavy_gate.try_acquire(
            heavy_gate.OWNER_FROZEN_OUTCOME_EVALUATION, parsed, environ=environ
        )
        if not acquired:
            return {"success": False, "error": gate_err or "HEAVY_JOB_RESOURCE_BUSY"}, 409
        _write_eval_files(
            directory,
            evaluation_id=parsed,
            source=source,
            created=_utcnow(),
            resume=True,
            outcome_data_end=request.get("outcome_data_end"),
            prev_status=status,
        )
        payload, code = _spawn(parsed, directory, environ=environ, spawn=spawn)
        if code != 200:
            heavy_gate.release(parsed, environ=environ)
        return payload, code


def handle_create_post(
    *,
    body: dict[str, Any],
    origin: str | None,
    referer: str | None,
    content_type: str | None,
    environ: dict | None = None,
    spawn: SpawnFn | None = None,
    disk_free: int | None = None,
) -> tuple[dict[str, Any], int]:
    guard = update_post_guard(origin=origin, referer=referer, content_type=content_type)
    if guard:
        return {"success": False, "error": guard}, 403
    try:
        parsed = FrozenFadeEvalBody(**body)
    except Exception:  # noqa: BLE001
        return {"success": False, "error": "UNKNOWN_FIELDS"}, 400
    return start_evaluation(
        parsed.source_job_id,
        environ=environ,
        spawn=spawn,
        disk_free=disk_free,
        outcome_data_end=getattr(parsed, "outcome_data_end", None),
    )


def handle_resume_post(
    *,
    evaluation_id: str,
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
    return resume_evaluation(evaluation_id, environ=environ, spawn=spawn)
