"""Exclusive start gate so candle-update and frozen-research jobs cannot race."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from stoch_universe_51.config import DASHBOARD_ROOT

DEFAULT_GATE = DASHBOARD_ROOT.parent / "results" / "stoch_dashboard_job_start.lock"


def start_gate_path(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_DASHBOARD_START_GATE") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_GATE


@contextmanager
def start_gate(environ: dict | None = None) -> Iterator[None]:
    """Process-wide exclusive lock around job-start check + ACTIVE.lock write.

    Both Frozen-research and candle-update starters must enter this gate before
    reading the other job's lock and before writing their own ACTIVE.lock.
    Holding the gate does not kill running jobs.
    """
    path = start_gate_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def frozen_research_active_id(environ: dict | None = None) -> str | None:
    from stoch_fade_research_jobs.jobs import active_job_id as fade_active

    return fade_active(environ)


def outcome_eval_active_id(environ: dict | None = None) -> str | None:
    from stoch_fade_research_evaluations.jobs import active_evaluation_id

    return active_evaluation_id(environ)
