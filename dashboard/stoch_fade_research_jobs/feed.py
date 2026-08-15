"""Read-only catalog and signal adapter for completed Frozen research jobs.

Does not import the signal engine, does not compute outcomes, does not write files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from stoch_universe_51.jsonio import read_json

from .complete import coin_run_is_complete
from .config import STRATEGY_VERSION, jobs_root
from .jobs import redact_public, safe_artifact_reference
from stoch_fade_research_evaluations.config import EXIT_POLICY as EVAL_EXIT_POLICY

SOURCE = "FROZEN_RESEARCH_JOB"
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SELECTABLE_STATES = frozenset({"COMPLETED", "COMPLETED_WITH_ERRORS"})
SIGNAL_FILE = "signals.jsonl"
MAX_LIMIT = 500
DEFAULT_LIMIT = 300
OUTCOME_NULL = {
    "wins": None,
    "losses": None,
    "open": None,
    "win_rate": None,
    "gross_profit": None,
    "gross_loss": None,
    "total_pnl": None,
}


def parse_job_id(raw: str) -> str | None:
    text = str(raw or "").strip().lower()
    if not JOB_ID_RE.fullmatch(text):
        return None
    if ".." in text or "/" in text or "\\" in text:
        return None
    return text


def resolve_job_dir(job_id: str, environ: dict | None = None) -> Path | None:
    parsed = parse_job_id(job_id)
    if parsed is None:
        return None
    root = jobs_root(environ).resolve()
    candidate = (root / parsed).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_dir():
        return None
    return candidate


def _iso_key(value: Any) -> str:
    text = str(value or "").replace("+00:00", "Z")
    return text


def _sort_ts(row: dict[str, Any]) -> str:
    return (
        _iso_key(row.get("confirmation_available_at"))
        or _iso_key(row.get("candle_close_time"))
        or _iso_key(row.get("entry_time"))
        or _iso_key(row.get("generated_at"))
        or ""
    )


def _parse_bool(raw: Any, default: bool = True) -> bool:
    if raw is None or raw == "":
        return default
    text = str(raw).strip().lower()
    if text in ("all", "*", "false", "0", "no"):
        return False
    if text in ("true", "1", "yes"):
        return True
    return default


def _job_request(directory: Path) -> dict[str, Any] | None:
    path = directory / "request.json"
    if not path.is_file() or path.suffix == ".tmp" or str(path).endswith(".tmp"):
        return None
    try:
        data = read_json(path)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return data


def _job_status(directory: Path) -> dict[str, Any] | None:
    path = directory / "status.json"
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return data


def _identity_ok(status: dict[str, Any], request: dict[str, Any]) -> bool:
    sv = str(request.get("fixed_strategy_version") or status.get("fixed_strategy_version") or "")
    if sv and sv != STRATEGY_VERSION:
        return False
    return True


def catalog_entry(directory: Path) -> dict[str, Any] | None:
    status = _job_status(directory)
    request = _job_request(directory)
    if status is None or request is None:
        return None
    job_id = parse_job_id(str(status.get("job_id") or directory.name))
    if job_id is None or job_id != directory.name:
        return None
    state = str(status.get("state") or "")
    if state not in SELECTABLE_STATES:
        return None
    if not _identity_ok(status, request):
        return None
    symbols = request.get("selected_symbols") or []
    if not isinstance(symbols, list):
        symbols = []
    symbols = [str(s) for s in symbols if s]
    combined = status.get("combined_summary") if isinstance(status.get("combined_summary"), dict) else {}
    return {
        "job_id": job_id,
        "state": state,
        "created_at": status.get("created_at"),
        "finished_at": status.get("finished_at"),
        "signal_start": request.get("signal_start"),
        "signal_end_exclusive": request.get("signal_end_exclusive"),
        "selected_symbols": symbols,
        "successful_coins": int(status.get("successful_coins") or combined.get("successful_coins") or 0),
        "failed_coins": int(status.get("failed_coins") or combined.get("failed_coins") or 0),
        "raw_total": int(status.get("raw_total") or combined.get("raw_candidates") or 0),
        "tier_a_total": int(status.get("tier_a_total") or combined.get("tier_a") or 0),
        "fixed_strategy_version": STRATEGY_VERSION,
        "outcome_evaluation_enabled": False,
        "execution_dedup_applied": False,
    }


def list_completed_jobs(environ: dict | None = None) -> list[dict[str, Any]]:
    root = jobs_root(environ)
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if parse_job_id(child.name) is None:
            continue
        entry = catalog_entry(child)
        if entry is None:
            continue
        rows.append(entry)
    rows.sort(
        key=lambda r: (_iso_key(r.get("finished_at")) or _iso_key(r.get("created_at")), r["job_id"]),
        reverse=True,
    )
    return redact_public(rows)


def catalog_response(environ: dict | None = None) -> dict[str, Any]:
    jobs = list_completed_jobs(environ)
    return {
        "success": True,
        "source": SOURCE,
        "jobs": jobs,
        "count": len(jobs),
        "implicit_latest": False,
        "outcomes_computed": False,
    }


def _coin_run_dir(job_dir: Path, symbol: str, runner_run_id: str) -> Path | None:
    ref = safe_artifact_reference(
        f"coin_runs/{symbol}/{runner_run_id}",
        symbol=symbol,
        runner_run_id=runner_run_id,
    )
    if not ref:
        return None
    run_dir = (job_dir / ref).resolve()
    try:
        run_dir.relative_to(job_dir.resolve())
    except ValueError:
        return None
    if not run_dir.is_dir():
        return None
    return run_dir


def _read_signals_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.name.endswith(".tmp") or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("signal_id"):
                rows.append(obj)
    return rows


def map_job_signal(
    raw: dict[str, Any],
    *,
    job_id: str,
    runner_run_id: str,
    job_start: str = "",
    job_end: str = "",
) -> dict[str, Any]:
    direction = str(raw.get("direction") or raw.get("trade_direction") or "").upper()
    return {
        "signal_id": raw.get("signal_id"),
        "symbol": raw.get("symbol"),
        "timeframe": raw.get("timeframe"),
        "direction": direction,
        "trade_direction": direction,
        "signal_type": raw.get("signal_type"),
        "tier_a": bool(raw.get("tier_a")),
        "is_q4": raw.get("is_q4"),
        "trend_bucket": raw.get("trend_bucket"),
        "eff_quantile": raw.get("eff_quantile"),
        "entry_valid": raw.get("entry_valid"),
        "entry_price": raw.get("entry_price"),
        "entry_time": raw.get("entry_time"),
        "tp_price": raw.get("tp_price"),
        "sl_price": raw.get("sl_price"),
        "candle_open_time": raw.get("candle_open_time"),
        "candle_close_time": raw.get("candle_close_time"),
        "confirmation_available_at": raw.get("confirmation_available_at"),
        "generated_at": raw.get("generated_at"),
        "strategy_version": STRATEGY_VERSION,
        "signal_state": "TIER_A" if raw.get("tier_a") else "CANDIDATE",
        "source": SOURCE,
        "job_id": job_id,
        "runner_run_id": runner_run_id,
        "outcomes_computed": False,
        "execution_dedup_applied": False,
        "be50_outcome_active": False,
        "plan_status": "PLANNED_NO_OUTCOME",
        "job_signal_start": job_start,
        "job_signal_end_exclusive": job_end,
    }


def _in_window(row: dict[str, Any], start: str, end: str) -> bool:
    ts = _sort_ts(row)[:19]
    if not ts:
        return True
    start_k = start.replace("+00:00", "Z")[:19]
    end_k = end.replace("+00:00", "Z")[:19]
    if start_k and ts < start_k:
        return False
    if end_k and ts >= end_k:
        return False
    return True


def load_job_signals(
    job_id: str,
    *,
    environ: dict | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    tier_a: Any = True,
    symbol: str | None = None,
    timeframe: str | None = None,
    direction: str | None = None,
    sort: str | None = None,
    evaluation_id: str | None = None,
) -> tuple[dict[str, Any] | None, int]:
    parsed = parse_job_id(job_id)
    if parsed is None:
        return {"success": False, "error": "JOB_ID_INVALID", "source": SOURCE}, 400
    job_dir = resolve_job_dir(parsed, environ)
    if job_dir is None:
        return {"success": False, "error": "JOB_NOT_FOUND", "source": SOURCE}, 404
    status = _job_status(job_dir)
    request = _job_request(job_dir)
    if status is None or request is None:
        return {"success": False, "error": "JOB_ARTIFACT_INVALID", "source": SOURCE}, 404
    state = str(status.get("state") or "")
    if state not in SELECTABLE_STATES:
        return {"success": False, "error": "JOB_NOT_SELECTABLE", "source": SOURCE, "state": state}, 409
    if not _identity_ok(status, request):
        return {"success": False, "error": "FROZEN_IDENTITY_MISMATCH", "source": SOURCE}, 409

    signal_start = str(request.get("signal_start") or "")
    signal_end = str(request.get("signal_end_exclusive") or "")
    selected = [str(s) for s in (request.get("selected_symbols") or []) if s]
    want_tier_a = _parse_bool(tier_a, default=True)
    want_symbol = str(symbol or "").strip().upper()
    want_tf = str(timeframe or "").strip()
    want_dir = str(direction or "").strip().upper()
    lim = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    off = max(0, int(offset or 0))
    reverse = str(sort or "desc").strip().lower() != "asc"

    mapped: list[dict[str, Any]] = []
    raw_total = 0
    tier_a_total = 0
    coins = status.get("coins") if isinstance(status.get("coins"), list) else []
    for coin in coins:
        if not isinstance(coin, dict):
            continue
        if str(coin.get("state") or "") != "COMPLETED":
            continue
        coin_symbol = str(coin.get("symbol") or "")
        if coin_symbol not in selected:
            continue
        run_id = str(coin.get("runner_run_id") or "")
        run_dir = _coin_run_dir(job_dir, coin_symbol, run_id)
        if run_dir is None:
            continue
        if not coin_run_is_complete(
            run_dir,
            symbol=coin_symbol,
            signal_start=signal_start,
            signal_end_exclusive=signal_end,
        ):
            continue
        signals_path = run_dir / SIGNAL_FILE
        if signals_path.name.endswith(".tmp"):
            continue
        rows = _read_signals_jsonl(signals_path)
        for raw in rows:
            if str(raw.get("symbol") or "") != coin_symbol:
                continue
            if str(raw.get("strategy_version") or STRATEGY_VERSION) != STRATEGY_VERSION:
                continue
            if not _in_window(raw, signal_start, signal_end):
                continue
            raw_total += 1
            if raw.get("tier_a"):
                tier_a_total += 1
            if want_tier_a and not raw.get("tier_a"):
                continue
            if want_symbol and str(raw.get("symbol") or "").upper() != want_symbol:
                continue
            if want_tf and str(raw.get("timeframe") or "") != want_tf:
                continue
            if want_dir and str(raw.get("direction") or "").upper() != want_dir:
                continue
            mapped.append(
                map_job_signal(
                    raw,
                    job_id=parsed,
                    runner_run_id=run_id,
                    job_start=signal_start,
                    job_end=signal_end,
                )
            )

    mapped.sort(key=_sort_ts, reverse=reverse)
    eval_meta = None
    if evaluation_id:
        from stoch_fade_research_evaluations.feed import load_outcomes_index

        ev_payload, ev_code = load_outcomes_index(evaluation_id, environ=environ)
        if ev_code != 200 or not ev_payload:
            return ev_payload, ev_code
        if str(ev_payload.get("source_job_id") or "") != parsed:
            return {"success": False, "error": "EVALUATION_JOB_MISMATCH", "source": SOURCE}, 409
        by_sid = ev_payload.get("by_signal_id") or {}
        joined: list[dict[str, Any]] = []
        for row in mapped:
            oc = by_sid.get(str(row.get("signal_id")))
            if not oc:
                joined.append(row)
                continue
            merged = dict(row)
            merged["source"] = "FROZEN_RESEARCH_EVALUATION"
            merged["outcomes_computed"] = True
            merged["evaluation_id"] = evaluation_id
            merged["result"] = oc.get("display_result") or oc.get("outcome")
            merged["display_result"] = oc.get("display_result") or oc.get("outcome")
            merged["pnl_pct"] = oc.get("pnl_pct_gross")
            merged["duration_seconds"] = oc.get("duration_seconds")
            merged["exit_time"] = oc.get("exit_time")
            merged["exit_price"] = oc.get("exit_price")
            merged["exit_reason"] = oc.get("exit_reason")
            merged["be50_activated"] = oc.get("be_activated")
            merged["be50_activated_at"] = oc.get("be_activation_time")
            merged["be_trigger_price"] = oc.get("be_trigger_price")
            merged["plan_status"] = None
            joined.append(merged)
        mapped = joined
        eval_meta = ev_payload.get("summary")
    page = mapped[off : off + lim]
    summary = {
        "raw_total": int(status.get("raw_total") or raw_total),
        "tier_a_total": int(status.get("tier_a_total") or tier_a_total),
        "filtered_signals": len(mapped),
        "signals": len(mapped),
        **OUTCOME_NULL,
        "strategy_version": STRATEGY_VERSION,
        "outcomes_computed": False,
    }
    if eval_meta:
        summary.update({k: eval_meta.get(k) for k in eval_meta})
        summary["outcomes_computed"] = True
        summary["pnl_basis"] = "gross"
    payload = {
        "success": True,
        "source": "FROZEN_RESEARCH_EVALUATION" if eval_meta else SOURCE,
        "job": {
            "job_id": parsed,
            "strategy_version": STRATEGY_VERSION,
            "signal_start": signal_start,
            "signal_end_exclusive": signal_end,
            "selected_symbols": selected,
            "state": state,
        },
        "outcomes_computed": bool(eval_meta),
        "execution_dedup_applied": False,
        "be50_outcome_active": False,
        "evaluation_id": parse_job_id(evaluation_id) if evaluation_id else None,
        "exit_policy": EVAL_EXIT_POLICY if eval_meta else None,
        "rows": page,
        "signals": page,
        "items": page,
        "pagination": {
            "limit": lim,
            "offset": off,
            "returned": len(page),
            "total": len(mapped),
        },
        "summary": summary,
        "feed_ready": True,
    }
    return redact_public(payload), 200


def frozen_strategy_requires_job(strategy_version: str | None) -> bool:
    return str(strategy_version or "").strip() == STRATEGY_VERSION
