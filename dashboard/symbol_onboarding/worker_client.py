"""Durable apply-queue client: dashboard enqueues only; systemd worker consumes."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import queue_dir
from .plans import canonical_request
from .schemas import OnboardForm

SpawnFn = Callable[[list[str], Path, Path], int]  # kept for tests / optional legacy

ACTIVE_STATUSES = frozenset({"QUEUED", "CLAIMED", "RUNNING"})
IN_FLIGHT_WORKER = frozenset({"CLAIMED", "RUNNING"})


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(
            "utf-8", "replace"
        )
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
    needles = ("symbol_onboarding/worker.py", "symbol_onboarding/queue_consumer.py", "queue_consumer.py")
    return any(n in cmd.replace("\\", "/") for n in needles) and (
        job_id in cmd or "queue_consumer" in cmd
    )


def lock_path(environ: dict | None = None) -> Path:
    return queue_dir(environ) / "ACTIVE.lock"


def _age_seconds(iso_ts: str | None) -> float:
    if not iso_ts:
        return 1e9
    try:
        text = str(iso_ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    except Exception:  # noqa: BLE001
        return 1e9


def reconcile_queue(environ: dict | None = None) -> str | None:
    """Return active job_id if queue busy.

    QUEUED jobs waiting for the systemd consumer are valid indefinitely.
    CLAIMED/RUNNING without a live consumer become FAILED/RECOVERY after grace.
    """
    path = lock_path(environ)
    if not path.is_file():
        # Still scan for in-flight without lock
        return _scan_inflight(environ)
    try:
        lock = _read_json(path)
    except Exception:  # noqa: BLE001
        path.unlink(missing_ok=True)
        return _scan_inflight(environ)
    job_id = str(lock.get("job_id") or "")
    pid = lock.get("pid")
    qdir = queue_dir(environ) / job_id
    status_path = qdir / "worker_status.json"
    status: dict[str, Any] = {}
    if status_path.is_file():
        try:
            status = _read_json(status_path)
        except Exception:  # noqa: BLE001
            status = {}
    state = str(status.get("state") or "")

    if state == "QUEUED":
        return job_id or None

    if worker_is_live(pid if isinstance(pid, int) else None, job_id):
        return job_id

    consumer_pid = status.get("consumer_pid") or status.get("pid")
    if worker_is_live(consumer_pid if isinstance(consumer_pid, int) else None, job_id):
        return job_id

    if state in IN_FLIGHT_WORKER:
        age = min(_age_seconds(status.get("started_at") or status.get("claimed_at")), _age_seconds(lock.get("started_at")))
        if age < 120.0:
            return job_id
        # Orphaned claimed/running — do not auto-retry apply
        status["state"] = "RECOVERY_REQUIRED" if state != "QUEUED" else "FAILED"
        status["finished_at"] = _utc_iso()
        status["error_code"] = "worker_aborted"
        status["message"] = "Onboarding-Worker abgebrochen (RECOVERY_REQUIRED, kein Auto-Retry)"
        _write_json(status_path, status)
        path.unlink(missing_ok=True)
        return None

    # Terminal or unknown — clear stale lock
    if state not in ACTIVE_STATUSES:
        path.unlink(missing_ok=True)
    return _scan_inflight(environ)


def _scan_inflight(environ: dict | None = None) -> str | None:
    root = queue_dir(environ)
    if not root.is_dir():
        return None
    for p in sorted(root.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_dir():
            continue
        sp = p / "worker_status.json"
        if not sp.is_file():
            continue
        try:
            st = _read_json(sp)
        except Exception:  # noqa: BLE001
            continue
        if str(st.get("state")) in ACTIVE_STATUSES:
            return p.name
    return None


def active_apply_job_id(environ: dict | None = None) -> str | None:
    return reconcile_queue(environ)


def default_spawn_worker(argv: list[str], cwd: Path, log_path: Path) -> int:
    """Legacy spawn path — disabled in production; kept for isolated unit tests."""
    raise RuntimeError("inline spawn disabled; use symbol-onboarding-worker.service")


def enqueue_apply(
    *,
    form: OnboardForm,
    plan_id: str,
    plan_hash: str,
    principal: str,
    environ: dict | None = None,
    spawn: SpawnFn | None = None,
    mode: str = "apply",
    fixture_sleep_sec: int | None = None,
) -> tuple[dict[str, Any], int]:
    """Write a validated queue job. Production: no spawn — systemd consumer picks up."""
    active = active_apply_job_id(environ)
    if active:
        return {
            "success": False,
            "error": "ONBOARDING_JOB_ALREADY_RUNNING",
            "job_id": active,
        }, 409

    from .config import jobs_dir

    lock_file = jobs_dir(environ) / ".onboarding.lock"
    if lock_file.is_file():
        try:
            payload = _read_json(lock_file)
            pid = payload.get("pid")
            if pid_alive(pid if isinstance(pid, int) else None):
                return {
                    "success": False,
                    "error": "ONBOARDING_LOCK_HELD",
                    "job_id": payload.get("job_id"),
                }, 409
        except Exception:  # noqa: BLE001
            pass

    if mode not in {"apply", "fixture"}:
        return {"success": False, "error": "BAD_MODE"}, 400
    if mode == "fixture" and os.environ.get("SYMBOL_ONBOARDING_ALLOW_FIXTURE", "").strip() != "1":
        # Allow via environ override for tests
        env = environ or {}
        if str(env.get("SYMBOL_ONBOARDING_ALLOW_FIXTURE") or "").strip() != "1":
            return {"success": False, "error": "FIXTURE_FORBIDDEN"}, 403

    job_id = f"{form.symbol}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    root = queue_dir(environ)
    root.mkdir(parents=True, exist_ok=True)
    directory = root / job_id
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return {"success": False, "error": "JOB_ID_COLLISION"}, 500

    request: dict[str, Any] = {
        "schema_version": "symbol_onboarding_apply_queue_v1",
        "job_id": job_id,
        "created_at": _utc_iso(),
        "principal": principal,
        "plan_id": plan_id,
        "plan_hash": plan_hash,
        "mode": mode,
        "request": canonical_request(form),
        "allowed_keys": [
            "symbol",
            "days",
            "candles_1m",
            "open_interest_5m",
            "public_trades",
            "with_ob1000",
            "restart_live",
            "purpose",
        ],
    }
    if mode == "fixture":
        request["fixture_sleep_sec"] = int(fixture_sleep_sec or 3)

    status = {
        "job_id": job_id,
        "state": "QUEUED",
        "pid": None,
        "consumer_pid": None,
        "started_at": None,
        "finished_at": None,
        "error_code": None,
        "message": "queued for symbol-onboarding-worker",
        "oa_job_id": job_id,
    }
    _write_json(directory / "request.json", request)
    _write_json(directory / "worker_status.json", status)
    (directory / "worker.log").write_text("", encoding="utf-8")
    _write_json(lock_path(environ), {"job_id": job_id, "pid": None, "started_at": _utc_iso()})

    # Optional legacy spawn only when explicitly requested by tests
    if spawn is not None:
        from .config import DASHBOARD_ROOT, dash_python

        python = str(dash_python(environ))
        worker = Path(__file__).resolve().parent / "worker.py"
        argv = [python, str(worker), job_id, str(directory)]
        try:
            pid = spawn(argv, DASHBOARD_ROOT, directory / "worker.log")
        except Exception as exc:  # noqa: BLE001
            status["state"] = "FAILED"
            status["finished_at"] = _utc_iso()
            status["error_code"] = "SPAWN_FAILED"
            status["message"] = str(exc)[:180]
            _write_json(directory / "worker_status.json", status)
            lock_path(environ).unlink(missing_ok=True)
            return {"success": False, "error": "SPAWN_FAILED", "job_id": job_id}, 500
        status["state"] = "RUNNING"
        status["pid"] = pid
        status["started_at"] = _utc_iso()
        status["message"] = "legacy spawn worker started"
        _write_json(directory / "worker_status.json", status)
        _write_json(
            lock_path(environ),
            {"job_id": job_id, "pid": pid, "started_at": status["started_at"]},
        )
        return {
            "success": True,
            "job_id": job_id,
            "state": "RUNNING",
            "message": "Onboarding-Job gestartet (legacy spawn)",
        }, 202

    return {
        "success": True,
        "job_id": job_id,
        "state": "QUEUED",
        "message": "Onboarding-Job in Queue — Worker übernimmt",
    }, 202


def read_queue_status(job_id: str, *, environ: dict | None = None) -> dict[str, Any] | None:
    text = str(job_id or "").strip()
    if not text or ".." in text or "/" in text:
        return None
    path = queue_dir(environ) / text / "worker_status.json"
    if not path.is_file():
        return None
    try:
        return _read_json(path)
    except Exception:  # noqa: BLE001
        return None


def enqueue_fixture(
    *,
    symbol: str = "FIXTUREUSDT",
    sleep_sec: int = 5,
    principal: str = "fixture",
    environ: dict | None = None,
) -> tuple[dict[str, Any], int]:
    """Test-only helper: enqueue a non-apply fixture job."""
    form = OnboardForm(
        symbol=symbol if symbol.endswith("USDT") else "FIXTUREUSDT",
        days=7,
        candles_1m=False,
        open_interest_5m=False,
        public_trades=False,
        with_ob1000=False,
        restart_live=False,
    )
    env = dict(environ or os.environ)
    env["SYMBOL_ONBOARDING_ALLOW_FIXTURE"] = "1"
    return enqueue_apply(
        form=form,
        plan_id="plan_fixture",
        plan_hash="fixture",
        principal=principal,
        environ=env,
        mode="fixture",
        fixture_sleep_sec=sleep_sec,
    )
