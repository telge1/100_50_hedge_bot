"""Async disk jobs for Nested Ask Pool Edge Short V1 (research-only).

Reuses the same pattern as EZM (status file + background thread) without
wiring into the Stochastic worker.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .nested_ask_pool_backtester import (
    RESULTS_ROOT,
    STRATEGY_ID,
    load_run_payload,
    run_nested_ask_pool_backtest,
    ui_summary_from_payload,
)
from .service import known_symbols

_LOCK = threading.Lock()
_ACTIVE: dict[str, str] = {}  # fingerprint -> job_id


def _jobs_root() -> Path:
    root = RESULTS_ROOT / "dashboard_jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _job_dir(job_id: str) -> Path:
    return _jobs_root() / job_id


def _fingerprint(symbol: str, start: str, end: str) -> str:
    raw = f"{STRATEGY_ID}|{symbol.upper()}|{start}|{end}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _write_status(job_id: str, payload: dict[str, Any]) -> None:
    path = _job_dir(job_id) / "status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _read_status(job_id: str) -> dict[str, Any] | None:
    path = _job_dir(job_id) / "status.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def start_nested_ask_pool_job(
    *,
    symbol: str,
    start: str,
    end: str,
    show_rejected: bool = False,
) -> tuple[dict[str, Any], int]:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {"success": False, "error": "symbol_required"}, 400
    if "," in sym or " " in sym:
        return {"success": False, "error": "single_symbol_required"}, 400
    if sym not in known_symbols():
        return {"success": False, "error": "unknown_symbol", "message": f"unknown symbol {sym}"}, 404
    start_s = str(start or "").strip()
    end_s = str(end or "").strip()
    if not start_s or not end_s:
        return {"success": False, "error": "start_end_required"}, 400

    fp = _fingerprint(sym, start_s, end_s)
    with _LOCK:
        existing = _ACTIVE.get(fp)
        if existing:
            st = _read_status(existing) or {}
            state = str(st.get("state") or "")
            if state in {"queued", "running"}:
                return {
                    "success": False,
                    "error": "DUPLICATE_ACTIVE_JOB",
                    "job_id": existing,
                    "state": state,
                    "message": "Identischer Nested-Job läuft bereits für Symbol/Zeitraum",
                }, 409

        job_id = f"nap_{int(time.time())}_{fp[:8]}"
        _ACTIVE[fp] = job_id
        status = {
            "success": True,
            "job_id": job_id,
            "strategy_id": STRATEGY_ID,
            "state": "queued",
            "progress_percent": 0,
            "symbol": sym,
            "symbols": [sym],
            "signal_start": start_s,
            "signal_end_exclusive": end_s,
            "show_rejected": bool(show_rejected),
            "fingerprint": fp,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "run_intent": "historical_backtest",
            "message": "queued",
        }
        jdir = _job_dir(job_id)
        jdir.mkdir(parents=True, exist_ok=True)
        (jdir / "request.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        _write_status(job_id, status)

    def _worker() -> None:
        _write_status(
            job_id,
            {
                **status,
                "state": "running",
                "progress_percent": 5,
                "message": "running engine",
                "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )
        try:
            result = run_nested_ask_pool_backtest(
                symbol=sym,
                start=start_s,
                end=end_s,
                show_rejected=show_rejected,
            )
            payload = load_run_payload(result["out_dir"])
            summary = ui_summary_from_payload(payload)
            done = {
                "success": True,
                "job_id": job_id,
                "strategy_id": STRATEGY_ID,
                "state": "completed",
                "progress_percent": 100,
                "symbol": sym,
                "symbols": [sym],
                "signal_start": start_s,
                "signal_end_exclusive": end_s,
                "show_rejected": bool(show_rejected),
                "fingerprint": fp,
                "out_dir": result.get("out_dir"),
                "run_id": result.get("run_id"),
                "summary": summary,
                "message": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            _write_status(job_id, done)
            ( _job_dir(job_id) / "result.json").write_text(
                json.dumps({"out_dir": result.get("out_dir"), "run_id": result.get("run_id")}, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            _write_status(
                job_id,
                {
                    "success": False,
                    "job_id": job_id,
                    "strategy_id": STRATEGY_ID,
                    "state": "failed",
                    "progress_percent": 100,
                    "symbol": sym,
                    "signal_start": start_s,
                    "signal_end_exclusive": end_s,
                    "fingerprint": fp,
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc()[-4000:],
                },
            )
        finally:
            with _LOCK:
                if _ACTIVE.get(fp) == job_id:
                    _ACTIVE.pop(fp, None)

    threading.Thread(target=_worker, name=f"nap-job-{job_id}", daemon=True).start()
    return status, 200


def nested_ask_pool_job_status(job_id: str) -> tuple[dict[str, Any], int]:
    jid = str(job_id or "").strip()
    if not jid:
        return {"success": False, "error": "job_id_required"}, 400
    st = _read_status(jid)
    if not st:
        return {"success": False, "error": "JOB_NOT_FOUND"}, 404
    if str(st.get("strategy_id") or "") != STRATEGY_ID:
        return {"success": False, "error": "NOT_NESTED_JOB"}, 409
    st["success"] = st.get("state") != "failed"
    return st, 200


def import_nested_ask_pool_job_to_workspace(job_id: str) -> tuple[dict[str, Any], int]:
    from .workspace_session import get_workspace

    st, code = nested_ask_pool_job_status(job_id)
    if code != 200:
        return st, code
    if str(st.get("state") or "") != "completed":
        return {
            "success": False,
            "error": "JOB_NOT_READY",
            "state": st.get("state"),
            "job_id": job_id,
        }, 409
    out_dir = st.get("out_dir")
    if not out_dir:
        res_path = _job_dir(job_id) / "result.json"
        if res_path.is_file():
            out_dir = json.loads(res_path.read_text(encoding="utf-8")).get("out_dir")
    if not out_dir:
        return {"success": False, "error": "MISSING_OUT_DIR"}, 500
    payload = load_run_payload(out_dir)
    ws = get_workspace()
    sym = str(payload.get("meta", {}).get("symbol") or st.get("symbol") or "").upper()
    for other in (
        "stoch_fade",
        "cluster_sweep_ema_9_20_59",
        "ema_dual_cross_multisource_v1",
        "ema_zone_microstructure_confirmation_v1",
        "a_plus_liquidity_pool_signal_scanner_v1",
    ):
        ws.clear_backtester_strategy(sym, strategy_id=other)
    snap = ws.store_nested_ask_pool_run(payload)
    snap["nested_ask_pool_result"] = {
        "meta": payload.get("meta"),
        "summary": ui_summary_from_payload(payload),
        "job_id": job_id,
        "n_overlays": len(payload.get("markers") or []),
    }
    snap["success"] = True
    return snap, 200
