#!/usr/bin/env python3
"""Durable symbol-onboarding apply worker.

Invoked only as:
  <dash-python> worker.py <job_id> <queue_dir>

Accepts a fixed request schema written by the dashboard adapter.
Never executes arbitrary shell or user-supplied argv.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DASHBOARD_ROOT = Path(__file__).resolve().parent.parent
if str(DASHBOARD_ROOT) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_ROOT))


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _update_status(directory: Path, **fields: Any) -> None:
    path = directory / "worker_status.json"
    status = _read_json(path) if path.is_file() else {}
    status.update(fields)
    _write_json(path, status)


def _clear_lock(job_id: str) -> None:
    from symbol_onboarding.worker_client import lock_path

    path = lock_path()
    if not path.is_file():
        return
    try:
        lock = _read_json(path)
    except Exception:  # noqa: BLE001
        path.unlink(missing_ok=True)
        return
    if str(lock.get("job_id") or "") == job_id:
        path.unlink(missing_ok=True)


def run(job_id: str, directory: Path) -> int:
    request_path = directory / "request.json"
    if not request_path.is_file():
        _update_status(
            directory,
            state="FAILED",
            finished_at=_utc_iso(),
            error_code="missing_request",
            message="request.json fehlt",
        )
        _clear_lock(job_id)
        return 2

    request = _read_json(request_path)
    if str(request.get("schema_version") or "") != "symbol_onboarding_apply_queue_v1":
        _update_status(
            directory,
            state="FAILED",
            finished_at=_utc_iso(),
            error_code="bad_schema",
            message="ungültiges Queue-Schema",
        )
        _clear_lock(job_id)
        return 2
    if str(request.get("job_id") or "") != job_id:
        _update_status(
            directory,
            state="FAILED",
            finished_at=_utc_iso(),
            error_code="job_id_mismatch",
            message="job_id stimmt nicht",
        )
        _clear_lock(job_id)
        return 2

    raw = request.get("request")
    if not isinstance(raw, dict):
        _update_status(
            directory,
            state="FAILED",
            finished_at=_utc_iso(),
            error_code="bad_request",
            message="request fehlt",
        )
        _clear_lock(job_id)
        return 2

    allowed = set(request.get("allowed_keys") or [])
    if not allowed.issubset(
        {
            "symbol",
            "days",
            "candles_1m",
            "open_interest_5m",
            "public_trades",
            "with_ob1000",
            "restart_live",
            "purpose",
        }
    ):
        _update_status(
            directory,
            state="FAILED",
            finished_at=_utc_iso(),
            error_code="forbidden_keys",
            message="Queue enthält unerlaubte Felder",
        )
        _clear_lock(job_id)
        return 2

    from symbol_onboarding.auth import auth_roles_for_onboard
    from symbol_onboarding.schemas import parse_onboard_form
    from symbol_onboarding.service_adapter import form_to_request, build_service
    from orderbook_analyse.symbol_onboarding.service import AuthContext, OnboardRejected

    form, err = parse_onboard_form(raw)
    if err or form is None:
        _update_status(
            directory,
            state="FAILED",
            finished_at=_utc_iso(),
            error_code=err or "invalid_form",
            message="Request ungültig",
        )
        _clear_lock(job_id)
        return 2

    principal = str(request.get("principal") or "worker")
    # Worker always maps as admin+symbol_onboard for AuthContext; HTTP already gated.
    roles = auth_roles_for_onboard({"role": "admin", "username": principal})
    service = build_service(enable_live_side_effects=True)
    req = form_to_request(form, apply=True, allow_live_restart=True)
    auth = AuthContext(principal=principal, roles=roles)

    _update_status(
        directory,
        state="RUNNING",
        pid=os.getpid(),
        started_at=_utc_iso(),
        message="OnboardService.run(apply=True)",
        oa_job_id=job_id,
    )

    try:
        job = service.run(req, auth=auth, job_id=job_id)
        _update_status(
            directory,
            state="COMPLETED",
            finished_at=_utc_iso(),
            message=str(job.get("final_verdict") or "done"),
            oa_job_id=job.get("job_id") or job_id,
            oa_status=job.get("status"),
        )
        _clear_lock(job_id)
        return 0
    except OnboardRejected as exc:
        _update_status(
            directory,
            state="FAILED",
            finished_at=_utc_iso(),
            error_code=getattr(exc, "code", "rejected"),
            message=str(exc)[:200],
            oa_job_id=job_id,
        )
        _clear_lock(job_id)
        return 1
    except Exception as exc:  # noqa: BLE001
        _update_status(
            directory,
            state="FAILED",
            finished_at=_utc_iso(),
            error_code="internal_error",
            message=f"{type(exc).__name__}",
            oa_job_id=job_id,
        )
        _clear_lock(job_id)
        return 1


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: worker.py <job_id> <queue_dir>", file=sys.stderr)
        return 2
    job_id = argv[1]
    directory = Path(argv[2]).resolve()
    if not directory.is_dir():
        print("queue dir missing", file=sys.stderr)
        return 2
    # Prevent path escape: job_id must match directory name
    if directory.name != job_id:
        print("job_id/dir mismatch", file=sys.stderr)
        return 2
    return run(job_id, directory)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
