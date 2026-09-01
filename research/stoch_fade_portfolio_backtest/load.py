from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import (
    ALLOWED_OUTCOMES,
    EVALS_ROOT,
    EXIT_POLICY,
    INTRABAR_POLICY,
    JOBS_ROOT,
    OUTCOME_ENGINE,
    SIGNAL_SCOPE,
    SIGNAL_STRATEGY_VERSION,
    UNIVERSE_PATH,
)
from .guards import require_id
from .io_util import file_fingerprint, iter_jsonl, read_json


def load_universe() -> list[str]:
    payload = read_json(UNIVERSE_PATH)
    symbols = payload.get("symbols") or payload.get("selected_symbols") or []
    return [str(s) for s in symbols]


def load_evaluation(evaluation_id: str) -> dict[str, Any]:
    eid = require_id(evaluation_id, "EVALUATION_ID")
    root = EVALS_ROOT / eid
    if not root.is_dir():
        raise ValueError("EVALUATION_NOT_FOUND")
    manifest = read_json(root / "evaluation_manifest.json")
    outcomes_path = root / "outcomes.jsonl"
    if not outcomes_path.is_file():
        raise ValueError("MISSING_OUTCOMES_JSONL")
    return {"evaluation_id": eid, "root": root, "manifest": manifest, "outcomes_path": outcomes_path}


def validate_evaluation_manifest(manifest: dict[str, Any], expected_job_id: str | None = None) -> list[str]:
    errors: list[str] = []
    if expected_job_id and str(manifest.get("source_job_id")) != expected_job_id:
        errors.append("SOURCE_JOB_MISMATCH")
    if str(manifest.get("signal_strategy_version") or "") != SIGNAL_STRATEGY_VERSION:
        errors.append("STRATEGY_MISMATCH")
    if str(manifest.get("signal_scope") or "") != SIGNAL_SCOPE:
        errors.append("SCOPE_MISMATCH")
    if str(manifest.get("exit_policy") or "") != EXIT_POLICY:
        errors.append("EXIT_POLICY_MISMATCH")
    engine = str(manifest.get("outcome_engine") or "")
    allowed_engines = {
        "evaluate_signal_no_be50_full_1m",
        "research.stoch_fade_evaluation.full_1m_scan.evaluate_signal_no_be50_full_1m",
        OUTCOME_ENGINE,
    }
    if engine not in allowed_engines and not engine.endswith("evaluate_signal_no_be50_full_1m"):
        errors.append("ENGINE_MISMATCH")
    if str(manifest.get("intrabar_policy") or "") != INTRABAR_POLICY:
        errors.append("INTRABAR_MISMATCH")
    if manifest.get("execution_dedup_applied") not in (False, None, False):
        if manifest.get("execution_dedup_applied") is True:
            errors.append("EXECUTION_DEDUP_APPLIED")
    return errors


def load_job(job_id: str) -> dict[str, Any]:
    jid = require_id(job_id, "JOB_ID")
    root = JOBS_ROOT / jid
    manifest = read_json(root / "job_manifest.json") if (root / "job_manifest.json").is_file() else {}
    if not manifest and (root / "manifest.json").is_file():
        manifest = read_json(root / "manifest.json")
    return {"job_id": jid, "root": root, "manifest": manifest}


def iter_job_signal_files(job_root: Path):
    coin_runs = job_root / "coin_runs"
    if not coin_runs.is_dir():
        return
    for symbol_dir in sorted(coin_runs.iterdir()):
        if not symbol_dir.is_dir():
            continue
        for run_dir in sorted(symbol_dir.iterdir()):
            sig = run_dir / "signals.jsonl"
            if sig.is_file():
                yield symbol_dir.name, sig


def load_tier_a_signals(job_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _symbol, path in iter_job_signal_files(job_root):
        for row in iter_jsonl(path):
            if row.get("tier_a") is True:
                rows.append(row)
    return rows


def load_outcomes(path: Path) -> list[dict[str, Any]]:
    return list(iter_jsonl(path))


def job_status_ok(manifest: dict[str, Any]) -> bool:
    status = str(manifest.get("status") or "").upper()
    return status in {"COMPLETED", "COMPLETE", "SUCCESS"}


def job_failed_coins(manifest: dict[str, Any]) -> int:
    for key in ("failed_coins", "failed_coin_count", "coins_failed"):
        if key in manifest:
            return int(manifest[key] or 0)
    summary = manifest.get("summary") or {}
    if isinstance(summary, dict) and "failed_coins" in summary:
        return int(summary["failed_coins"] or 0)
    return 0
