"""Read-only catalog and outcome pages for Frozen-signal NO_BE50 evaluations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stoch_universe_51.jsonio import read_json

from stoch_fade_research_jobs.feed import JOB_ID_RE, parse_job_id
from stoch_fade_research_jobs.jobs import redact_public

from .artifacts import load_combined_summary, read_outcomes_prefer_root
from .config import EXIT_POLICY, SIGNAL_SCOPE, SOURCE, STRATEGY_VERSION, evaluations_root

SELECTABLE = frozenset({"COMPLETED", "COMPLETED_WITH_ERRORS"})
MAX_LIMIT = 500
DEFAULT_LIMIT = 300


def _stored_exit_policy(directory: Path) -> str:
    req = directory / "request.json"
    if req.is_file():
        try:
            data = read_json(req)
            return str(data.get("exit_policy") or "")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def list_evaluations(environ: dict | None = None, *, source_job_id: str | None = None) -> list[dict[str, Any]]:
    root = evaluations_root(environ)
    if not root.is_dir():
        return []
    want = parse_job_id(source_job_id) if source_job_id else None
    rows: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir() or not JOB_ID_RE.fullmatch(child.name):
            continue
        status_path = child / "status.json"
        if not status_path.is_file():
            continue
        try:
            status = read_json(status_path)
        except Exception:  # noqa: BLE001
            continue
        if str(status.get("state") or "") not in SELECTABLE:
            continue
        if _stored_exit_policy(child) != EXIT_POLICY:
            continue
        if want and str(status.get("source_job_id") or "") != want:
            continue
        combined = load_combined_summary(child, status)
        rows.append(
            {
                "evaluation_id": child.name,
                "source_job_id": status.get("source_job_id"),
                "state": status.get("state"),
                "created_at": status.get("created_at"),
                "finished_at": status.get("finished_at"),
                "tier_a_total": status.get("tier_a_total") or combined.get("signals"),
                "wins": combined.get("wins", status.get("wins")),
                "losses": combined.get("losses", status.get("losses")),
                "open": combined.get("open", status.get("open")),
                "win_rate_pct": combined.get("win_rate_pct"),
                "exit_policy": EXIT_POLICY,
                "signal_strategy_version": STRATEGY_VERSION,
                "outcome_engine": "evaluate_signal_no_be50",
                "intrabar_policy": "SL_FIRST",
                "signal_scope": SIGNAL_SCOPE,
                "execution_dedup_applied": False,
                "fixed_strategy_version": STRATEGY_VERSION,
            }
        )
    rows.sort(key=lambda r: (str(r.get("finished_at") or ""), r["evaluation_id"]), reverse=True)
    return redact_public(rows)


def catalog_response(environ: dict | None = None, source_job_id: str | None = None) -> dict[str, Any]:
    jobs = list_evaluations(environ, source_job_id=source_job_id)
    return {
        "success": True,
        "source": SOURCE,
        "evaluations": jobs,
        "count": len(jobs),
        "implicit_latest": False,
        "exit_policy": EXIT_POLICY,
    }


def _coin_order(directory: Path, status: dict[str, Any]) -> list[dict[str, Any]]:
    index_path = directory / "source_index.json"
    if index_path.is_file():
        try:
            index = read_json(index_path)
            coins = index.get("coins") if isinstance(index, dict) else None
            if isinstance(coins, list) and coins:
                return coins
        except Exception:  # noqa: BLE001
            pass
    coins = status.get("coins") if isinstance(status.get("coins"), list) else []
    return [c for c in coins if isinstance(c, dict)]


def _read_outcomes(directory: Path, status: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return read_outcomes_prefer_root(directory, _coin_order(directory, status or {}))


def load_outcomes_index(evaluation_id: str, *, environ: dict | None = None) -> tuple[dict[str, Any] | None, int]:
    parsed = parse_job_id(evaluation_id)
    if parsed is None:
        return {"success": False, "error": "JOB_ID_INVALID", "source": SOURCE}, 400
    directory = evaluations_root(environ) / parsed
    if not directory.is_dir():
        return {"success": False, "error": "JOB_NOT_FOUND", "source": SOURCE}, 404
    status = read_json(directory / "status.json") if (directory / "status.json").is_file() else {}
    if str(status.get("state") or "") not in SELECTABLE:
        return {"success": False, "error": "EVAL_NOT_SELECTABLE", "source": SOURCE}, 409
    if _stored_exit_policy(directory) != EXIT_POLICY:
        return {"success": False, "error": "EVALUATION_POLICY_MISMATCH", "source": SOURCE}, 409
    rows = _read_outcomes(directory, status)
    by_sid = {str(r.get("signal_id")): r for r in rows if r.get("signal_id")}
    summary = load_combined_summary(directory, status)
    return {
        "success": True,
        "source": SOURCE,
        "evaluation_id": parsed,
        "source_job_id": status.get("source_job_id"),
        "rows": rows,
        "by_signal_id": by_sid,
        "summary": summary,
    }, 200


def load_outcomes(
    evaluation_id: str,
    *,
    environ: dict | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    symbol: str | None = None,
    timeframe: str | None = None,
    direction: str | None = None,
) -> tuple[dict[str, Any] | None, int]:
    parsed = parse_job_id(evaluation_id)
    if parsed is None:
        return {"success": False, "error": "JOB_ID_INVALID", "source": SOURCE}, 400
    directory = evaluations_root(environ) / parsed
    if not directory.is_dir():
        return {"success": False, "error": "JOB_NOT_FOUND", "source": SOURCE}, 404
    status = read_json(directory / "status.json") if (directory / "status.json").is_file() else {}
    if str(status.get("state") or "") not in SELECTABLE:
        return {"success": False, "error": "EVAL_NOT_SELECTABLE", "source": SOURCE}, 409
    if _stored_exit_policy(directory) != EXIT_POLICY:
        return {"success": False, "error": "EVALUATION_POLICY_MISMATCH", "source": SOURCE}, 409
    rows = _read_outcomes(directory, status)
    want_symbol = str(symbol or "").strip().upper()
    want_tf = str(timeframe or "").strip()
    want_dir = str(direction or "").strip().upper()
    filtered = []
    for row in rows:
        if want_symbol and str(row.get("symbol") or "").upper() != want_symbol:
            continue
        if want_tf and str(row.get("timeframe") or "") != want_tf:
            continue
        if want_dir and str(row.get("direction") or "").upper() != want_dir:
            continue
        filtered.append(row)
    filtered.sort(key=lambda r: str(r.get("entry_time") or r.get("exit_time") or ""), reverse=True)
    lim = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    off = max(0, int(offset or 0))
    page = filtered[off : off + lim]
    summary = load_combined_summary(directory, status)
    payload = {
        "success": True,
        "source": SOURCE,
        "evaluation_id": parsed,
        "source_job_id": status.get("source_job_id"),
        "exit_policy": EXIT_POLICY,
        "signal_scope": SIGNAL_SCOPE,
        "execution_dedup_applied": False,
        "outcomes_computed": True,
        "rows": page,
        "outcomes": page,
        "pagination": {"limit": lim, "offset": off, "returned": len(page), "total": len(filtered)},
        "summary": {
            "signals": summary.get("signals"),
            "wins": summary.get("wins"),
            "losses": summary.get("losses"),
            "open": summary.get("open"),
            "win_rate_pct": summary.get("win_rate_pct"),
            "gross_profit_pct": summary.get("gross_profit_pct"),
            "gross_loss_pct": summary.get("gross_loss_pct"),
            "total_pnl_pct": summary.get("total_pnl_pct"),
            "pnl_basis": "gross",
            "be50_activated_count": summary.get("be50_activated_count"),
            "be50_exit_count": summary.get("be50_exit_count"),
            "strategy_version": STRATEGY_VERSION,
        },
    }
    return redact_public(payload), 200
