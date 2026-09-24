"""Queue consumer recovery / fixture tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1]
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from symbol_onboarding.queue_consumer import reconcile_orphans, loop  # noqa: E402
from symbol_onboarding.worker_client import enqueue_fixture, read_queue_status  # noqa: E402


def test_orphan_apply_marked_recovery_required(tmp_path, monkeypatch):
    queue = tmp_path / "queue"
    jobs = tmp_path / "jobs"
    queue.mkdir()
    jobs.mkdir()
    monkeypatch.setenv("SYMBOL_ONBOARDING_QUEUE_DIR", str(queue))
    monkeypatch.setenv("SYMBOL_ONBOARDING_JOBS_DIR", str(jobs))
    job_id = "AAPLUSDT_20260101T000000Z_orphan01"
    d = queue / job_id
    d.mkdir()
    (d / "request.json").write_text(
        json.dumps(
            {
                "schema_version": "symbol_onboarding_apply_queue_v1",
                "job_id": job_id,
                "mode": "apply",
                "request": {"symbol": "AAPLUSDT", "days": 7},
                "allowed_keys": ["symbol", "days"],
            }
        ),
        encoding="utf-8",
    )
    (d / "worker_status.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "state": "RUNNING",
                "consumer_pid": 99999999,
                "started_at": "2020-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    n = reconcile_orphans(environ={"SYMBOL_ONBOARDING_QUEUE_DIR": str(queue)})
    assert n == 1
    st = json.loads((d / "worker_status.json").read_text())
    assert st["state"] == "RECOVERY_REQUIRED"
    assert st["error_code"] == "orphan_apply"


def test_fixture_once_completes(tmp_path, monkeypatch):
    queue = tmp_path / "queue"
    jobs = tmp_path / "jobs"
    queue.mkdir()
    jobs.mkdir()
    env = {
        "SYMBOL_ONBOARDING_QUEUE_DIR": str(queue),
        "SYMBOL_ONBOARDING_JOBS_DIR": str(jobs),
        "SYMBOL_ONBOARDING_ALLOW_FIXTURE": "1",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    payload, code = enqueue_fixture(sleep_sec=1, environ=env)
    assert code == 202
    rc = loop(poll_sec=0.2, once=True)
    assert rc == 0
    st = read_queue_status(payload["job_id"], environ=env)
    assert st["state"] == "COMPLETED"
