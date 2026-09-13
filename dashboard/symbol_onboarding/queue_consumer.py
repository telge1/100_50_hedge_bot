"""Long-running queue consumer for symbol onboarding (systemd user unit).

Claims at most one QUEUED job, runs OnboardService.apply or a fixture sleep,
never executes arbitrary shell/modules from the queue.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DASHBOARD_ROOT = Path(__file__).resolve().parent.parent
if str(DASHBOARD_ROOT) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_ROOT))

from symbol_onboarding.config import queue_dir  # noqa: E402
from symbol_onboarding.sanitization import validate_job_id  # noqa: E402
from symbol_onboarding.worker_client import (  # noqa: E402
    ACTIVE_STATUSES,
    lock_path,
    pid_alive,
    _read_json,
    _write_json,
    _utc_iso,
)

STOP = False

TERMINAL = frozenset({"COMPLETED", "FAILED", "RECOVERY_REQUIRED"})
CLAIMABLE = frozenset({"QUEUED"})
IN_FLIGHT = frozenset({"CLAIMED", "RUNNING", "QUEUED"})


def _log(msg: str) -> None:
    print(f"[symbol-onboarding-worker] {_utc_iso()} {msg}", flush=True)


def _handle_stop(signum: int, _frame: Any) -> None:
    global STOP
    STOP = True
    _log(f"signal {signum} received — stopping after current job")


def _job_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out = []
    for p in root.iterdir():
        if not p.is_dir():
            continue
        if validate_job_id(p.name) is None:
            continue
        if (p / "request.json").is_file() and (p / "worker_status.json").is_file():
            out.append(p)
    return sorted(out, key=lambda d: d.stat().st_mtime)


def _mark_recovery(directory: Path, *, code: str, message: str) -> None:
    status = _read_json(directory / "worker_status.json")
    if str(status.get("state")) in TERMINAL:
        return
    # Never auto-retry partially activated apply jobs.
    status["state"] = "RECOVERY_REQUIRED" if code == "orphan_apply" else "FAILED"
    status["finished_at"] = _utc_iso()
    status["error_code"] = code
    status["message"] = message[:200]
    _write_json(directory / "worker_status.json", status)
    job_id = str(status.get("job_id") or directory.name)
    lp = lock_path()
    if lp.is_file():
        try:
            lock = _read_json(lp)
            if str(lock.get("job_id") or "") == job_id:
                lp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            lp.unlink(missing_ok=True)


def reconcile_orphans(*, environ: dict | None = None) -> int:
    """On startup: orphaned CLAIMED/RUNNING → RECOVERY_REQUIRED/FAILED (no re-apply)."""
    root = queue_dir(environ)
    fixed = 0
    for directory in _job_dirs(root):
        status = _read_json(directory / "worker_status.json")
        state = str(status.get("state") or "")
        if state not in {"CLAIMED", "RUNNING"}:
            continue
        pid = status.get("consumer_pid") or status.get("pid")
        if pid_alive(pid if isinstance(pid, int) else None):
            # Another live consumer owns it.
            continue
        req = {}
        try:
            req = _read_json(directory / "request.json")
        except Exception:  # noqa: BLE001
            req = {}
        mode = str(req.get("mode") or "apply")
        if mode == "fixture":
            _mark_recovery(
                directory,
                code="orphan_fixture",
                message="Fixture-Worker abgebrochen; kein Re-Apply",
            )
        else:
            _mark_recovery(
                directory,
                code="orphan_apply",
                message="Apply-Worker abgebrochen — RECOVERY_REQUIRED (kein Auto-Retry)",
            )
        fixed += 1
        _log(f"orphan marked {directory.name} mode={mode}")
    return fixed


def _try_claim(directory: Path) -> dict[str, Any] | None:
    """Atomically claim a QUEUED job for this consumer PID."""
    status_path = directory / "worker_status.json"
    try:
        status = _read_json(status_path)
    except Exception:  # noqa: BLE001
        return None
    if str(status.get("state")) != "QUEUED":
        return None
    # Exclusive claim via O_EXCL claim file
    claim = directory / f".claim.{os.getpid()}"
    final_claim = directory / "CLAIMED.by"
    try:
        fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        return None
    except OSError:
        return None
    if final_claim.exists():
        claim.unlink(missing_ok=True)
        return None
    try:
        claim.replace(final_claim)
    except OSError:
        claim.unlink(missing_ok=True)
        return None

    status["state"] = "CLAIMED"
    status["consumer_pid"] = os.getpid()
    status["claimed_at"] = _utc_iso()
    status["message"] = "claimed by systemd worker"
    _write_json(status_path, status)
    _write_json(
        lock_path(),
        {
            "job_id": directory.name,
            "pid": os.getpid(),
            "started_at": _utc_iso(),
            "consumer": "symbol-onboarding-worker",
        },
    )
    return status


def _run_fixture(directory: Path, request: dict[str, Any]) -> int:
    if os.environ.get("SYMBOL_ONBOARDING_ALLOW_FIXTURE", "").strip() != "1":
        _mark_recovery(
            directory,
            code="fixture_forbidden",
            message="Fixture-Modus nicht freigeschaltet",
        )
        return 2
    sleep_sec = int(request.get("fixture_sleep_sec") or 3)
    sleep_sec = max(1, min(sleep_sec, 120))
    status_path = directory / "worker_status.json"
    status = _read_json(status_path)
    status["state"] = "RUNNING"
    status["started_at"] = _utc_iso()
    status["pid"] = os.getpid()
    status["consumer_pid"] = os.getpid()
    status["message"] = f"fixture sleep {sleep_sec}s"
    _write_json(status_path, status)
    end = time.time() + sleep_sec
    while time.time() < end and not STOP:
        time.sleep(0.2)
    if STOP:
        status = _read_json(status_path)
        status["state"] = "FAILED"
        status["finished_at"] = _utc_iso()
        status["error_code"] = "stopped"
        status["message"] = "fixture interrupted by stop"
        _write_json(status_path, status)
        return 1
    status = _read_json(status_path)
    status["state"] = "COMPLETED"
    status["finished_at"] = _utc_iso()
    status["progress_pct"] = 100
    status["message"] = "fixture completed"
    status["oa_status"] = "FIXTURE_OK"
    _write_json(status_path, status)
    return 0


def _run_apply(directory: Path, job_id: str) -> int:
    from symbol_onboarding.worker import run as run_one

    status_path = directory / "worker_status.json"
    status = _read_json(status_path)
    status["state"] = "RUNNING"
    status["started_at"] = status.get("started_at") or _utc_iso()
    status["pid"] = os.getpid()
    status["consumer_pid"] = os.getpid()
    status["message"] = "OnboardService.run(apply=True)"
    _write_json(status_path, status)
    # worker.run clears lock on completion
    return int(run_one(job_id, directory))


def process_one(directory: Path) -> int:
    job_id = directory.name
    if validate_job_id(job_id) is None:
        return 2
    request = _read_json(directory / "request.json")
    if str(request.get("schema_version") or "") != "symbol_onboarding_apply_queue_v1":
        _mark_recovery(directory, code="bad_schema", message="ungültiges Queue-Schema")
        return 2
    if str(request.get("job_id") or "") != job_id:
        _mark_recovery(directory, code="job_id_mismatch", message="job_id stimmt nicht")
        return 2
    mode = str(request.get("mode") or "apply")
    try:
        if mode == "fixture":
            rc = _run_fixture(directory, request)
        elif mode == "apply":
            rc = _run_apply(directory, job_id)
        else:
            _mark_recovery(directory, code="bad_mode", message=f"unsupported mode {mode}")
            rc = 2
    finally:
        # Ensure lock cleared if still ours
        lp = lock_path()
        if lp.is_file():
            try:
                lock = _read_json(lp)
                if str(lock.get("job_id") or "") == job_id:
                    lp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    return rc


def loop(*, poll_sec: float = 2.0, once: bool = False) -> int:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    environ = dict(os.environ)
    root = queue_dir(environ)
    root.mkdir(parents=True, exist_ok=True)
    _log(f"queue={root} pid={os.getpid()}")
    n = reconcile_orphans(environ=environ)
    _log(f"reconciled_orphans={n}")

    while not STOP:
        claimed = None
        for directory in _job_dirs(root):
            status = _read_json(directory / "worker_status.json")
            if str(status.get("state")) not in CLAIMABLE:
                continue
            claimed = _try_claim(directory)
            if claimed is not None:
                _log(f"claimed {directory.name}")
                rc = process_one(directory)
                _log(f"finished {directory.name} rc={rc}")
                break
        if once:
            return 0
        # Idle sleep; empty queue is fine
        for _ in range(int(max(1, poll_sec / 0.2))):
            if STOP:
                break
            time.sleep(0.2)
    _log("exit clean")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Symbol onboarding queue consumer")
    parser.add_argument("--once", action="store_true", help="Process at most one job then exit")
    parser.add_argument("--poll-sec", type=float, default=2.0)
    parser.add_argument("--reconcile-only", action="store_true")
    args = parser.parse_args(argv)
    if args.reconcile_only:
        return 0 if reconcile_orphans() >= 0 else 1
    return loop(poll_sec=args.poll_sec, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
