"""Research Charts adapter: start/poll/import EZM jobs via stoch job infra."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stoch_fade_research_jobs.config import EZM_STRATEGY_ID, normalize_ezm_computation_mode
from stoch_fade_research_jobs.ezm_adapter import load_ezm_research_layers_from_run_dir
from stoch_fade_research_jobs.feed import load_job_signals
from stoch_fade_research_jobs.jobs import job_dir_for, load_job_public, start_frozen_job
from stoch_fade_research_jobs.time_window import iso_z, parse_utc_minute, suggested_end_exclusive
from stoch_universe_51.jsonio import read_json

from .ezm_backtester import STRATEGY_ID, research_layers_to_marker_specs, signal_rows_to_marker_specs
from .service import known_symbols
from .workspace_session import get_workspace


def _floor_minute(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    return dt.replace(second=0, microsecond=0)


def _parse_pair(start_raw: Any, end_raw: Any) -> tuple[str, str]:
    def one(raw: Any) -> datetime:
        text = str(raw or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        # datetime-local style YYYY-MM-DDTHH:MM[:SS]
        if len(text) == 16 and "T" in text:
            text = text + ":00+00:00"
        elif len(text) == 19 and "T" in text and "+" not in text and not text.endswith("Z"):
            text = text + "+00:00"
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _floor_minute(dt.astimezone(timezone.utc))

    start = one(start_raw)
    end = one(end_raw)
    if end <= start:
        raise ValueError("START_NOT_BEFORE_END")
    return iso_z(start), iso_z(end)


def start_ezm_research_job(
    *,
    symbol: str,
    start: str,
    end: str,
    computation_mode: str | None = None,
    environ: dict | None = None,
) -> tuple[dict[str, Any], int]:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {"success": False, "error": "symbol_required"}, 400
    if "," in sym or " " in sym:
        return {"success": False, "error": "single_symbol_required"}, 400
    if sym not in known_symbols():
        return {"success": False, "error": "unknown_symbol", "message": f"unknown symbol {sym}"}, 404
    try:
        start_iso, end_iso = _parse_pair(start, end)
        end_dt = parse_utc_minute(end_iso)
        max_end = suggested_end_exclusive()
        if end_dt > max_end:
            end_iso = iso_z(max_end)
        if parse_utc_minute(end_iso) <= parse_utc_minute(start_iso):
            return {"success": False, "error": "START_NOT_BEFORE_END"}, 400
    except ValueError as exc:
        return {"success": False, "error": str(exc)}, 400

    try:
        resolved_computation_mode = normalize_ezm_computation_mode(computation_mode)
    except ValueError:
        return {"success": False, "error": "INVALID_COMPUTATION_MODE"}, 400

    payload, code = start_frozen_job(
        [sym],
        start_iso,
        end_iso,
        strategy_id=EZM_STRATEGY_ID,
        computation_mode=resolved_computation_mode,
        environ=environ,
    )
    if code != 200:
        return payload, code
    payload["strategy_id"] = EZM_STRATEGY_ID
    payload["run_intent"] = "candidate_discovery"
    payload["symbol"] = sym
    payload["symbols"] = [sym]
    payload["signal_start"] = start_iso
    payload["signal_end_exclusive"] = end_iso
    payload["computation_mode"] = resolved_computation_mode
    return payload, code


def _job_request(job_id: str, environ: dict | None) -> dict[str, Any]:
    directory = job_dir_for(job_id, environ)
    req_path = directory / "request.json"
    if not req_path.exists():
        return {}
    try:
        data = read_json(req_path)
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def ezm_job_status(job_id: str, *, environ: dict | None = None) -> tuple[dict[str, Any], int]:
    public = load_job_public(job_id, environ)
    if not public or not public.get("job_id"):
        return {"success": False, "error": "JOB_NOT_FOUND"}, 404
    request = _job_request(job_id, environ)
    sid = str(request.get("strategy_id") or public.get("strategy_id") or "")
    if sid and sid != EZM_STRATEGY_ID:
        return {"success": False, "error": "NOT_EZM_JOB"}, 409
    if not sid:
        # Reject Frozen Fade / unknown jobs that lack EZM strategy_id
        return {"success": False, "error": "NOT_EZM_JOB"}, 409
    public["success"] = True
    public["strategy_id"] = EZM_STRATEGY_ID
    public["run_intent"] = str(request.get("run_intent") or "candidate_discovery")
    public["signal_start"] = request.get("signal_start")
    public["signal_end_exclusive"] = request.get("signal_end_exclusive")
    public["symbols"] = list(request.get("symbols") or request.get("selected_symbols") or [])
    public["computation_mode"] = request.get("computation_mode")
    public["incomplete_coins"] = public.get("incomplete_coins")
    return public, 200


def _coin_run_dir(job_id: str, symbol: str, environ: dict | None) -> Path | None:
    status, _ = ezm_job_status(job_id, environ=environ)
    if not status:
        return None
    coins = status.get("coins") or []
    run_id = None
    for c in coins:
        if isinstance(c, dict) and str(c.get("symbol") or "").upper() == symbol.upper():
            run_id = c.get("runner_run_id")
            break
    if not run_id:
        return None
    job_dir = job_dir_for(job_id, environ)
    run_dir = job_dir / "coin_runs" / symbol.upper() / str(run_id)
    return run_dir if run_dir.is_dir() else None


def import_ezm_job_to_workspace(
    *,
    job_id: str,
    symbol: str,
    environ: dict | None = None,
) -> tuple[dict[str, Any], int]:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {"success": False, "error": "symbol_required"}, 400
    status, scode = ezm_job_status(job_id, environ=environ)
    if scode != 200:
        return status, scode
    state = str(status.get("state") or "")
    if state not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        return {
            "success": False,
            "error": "JOB_NOT_READY",
            "state": state,
            "job_id": job_id,
        }, 409

    feed, fcode = load_job_signals(
        job_id,
        environ=environ,
        symbol=sym,
        tier_a=True,
        limit=5000,
        offset=0,
    )
    if fcode != 200 or not feed:
        return feed or {"success": False, "error": "SIGNAL_LOAD_FAILED"}, fcode

    rows = list(feed.get("rows") or feed.get("signals") or [])
    run_dir = _coin_run_dir(job_id, sym, environ)
    layers = (
        load_ezm_research_layers_from_run_dir(run_dir)
        if run_dir is not None
        else {"ema_setup_events": [], "microstructure_confirmation_events": [], "candidates": []}
    )
    ema_setup_rows = list(layers.get("ema_setup_events") or [])
    micro_rows = list(layers.get("microstructure_confirmation_events") or [])
    if not micro_rows and layers.get("candidates"):
        micro_rows = list(layers.get("candidates") or [])
    if not micro_rows:
        micro_rows = rows
    markers = research_layers_to_marker_specs(
        ema_setup_rows=ema_setup_rows,
        micro_rows=micro_rows,
        show_ema_setup=True,
        show_microstructure=True,
    )
    if not markers and rows:
        markers = signal_rows_to_marker_specs(rows)
    coin_rows = []
    for c in status.get("coins") or []:
        if isinstance(c, dict) and str(c.get("symbol") or "").upper() == sym:
            coin_rows.append(c)
    coverage = {
        "symbol": sym,
        "job_id": job_id,
        "signal_start": status.get("signal_start") or (feed.get("job") or {}).get("signal_start"),
        "signal_end_exclusive": status.get("signal_end_exclusive")
        or (feed.get("job") or {}).get("signal_end_exclusive"),
        "coin": coin_rows[0] if coin_rows else None,
        "incomplete_coins": status.get("incomplete_coins"),
        "failed_coins": status.get("failed_coins"),
    }
    payload = {
        "meta": {
            "symbol": sym,
            "strategy_id": STRATEGY_ID,
            "run_intent": "candidate_discovery",
            "job_id": job_id,
            "signal_start": coverage["signal_start"],
            "signal_end_exclusive": coverage["signal_end_exclusive"],
            "n_markers": len(markers),
            "n_setup_markers": len([m for m in markers if m.get("layer") == "ema_setup"]),
            "n_micro_markers": len(
                [m for m in markers if m.get("layer") == "microstructure_confirmation"]
            ),
            "n_rows": len(rows),
            "n_ema_setup_events": len(ema_setup_rows),
            "n_micro_events": len(micro_rows),
        },
        "ema_setup_events": ema_setup_rows,
        "microstructure_confirmation_events": micro_rows,
        "candidates": micro_rows or rows,
        "markers": markers,
        "coverage": coverage,
        "summary": feed.get("summary") or {},
        "strategy_id": STRATEGY_ID,
    }
    ws = get_workspace()
    ws.clear_backtester_strategy(sym, strategy_id="cluster_sweep_ema_9_20_59")
    ws.clear_backtester_strategy(sym, strategy_id="ema_dual_cross_multisource_v1")
    ws.clear_backtester_strategy(sym, strategy_id="stoch_fade")
    snap = ws.store_ezm_run(payload)
    snap["ezm_result"] = {
        "meta": payload["meta"],
        "coverage": coverage,
        "summary": payload["summary"],
        "n_markers": len(markers),
        "n_candidates": len(rows),
    }
    snap["success"] = True
    return snap, 200
