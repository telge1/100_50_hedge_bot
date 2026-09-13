"""Sanitize job / error payloads for browser clients."""

from __future__ import annotations

import re
from typing import Any

from .config import STAGE_LABELS_DE

SECRET_MARKERS = (
    "PASSWORD",
    "SECRET",
    "API_KEY",
    "TOKEN",
    "BYBIT_KEY",
    "CLICKHOUSE_PASSWORD",
    "AUTHORIZATION",
    "PRIVATE_KEY",
    "BEGIN RSA",
    "/.ENV",
)

_TRACE_RE = re.compile(r"Traceback \(most recent call last\):", re.I)
_PATH_RE = re.compile(r"(/home/[^\s\"']+)")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,120}$")


def public_message(text: str, limit: int = 240) -> str:
    raw = " ".join(str(text or "").split())
    upper = raw.upper()
    for marker in SECRET_MARKERS:
        if marker in upper:
            return "Vorgang fehlgeschlagen"
    if _TRACE_RE.search(raw):
        return "Vorgang fehlgeschlagen"
    cleaned = _PATH_RE.sub("[path]", raw)
    return cleaned[:limit]


def validate_job_id(job_id: str) -> str | None:
    text = str(job_id or "").strip()
    if not text or ".." in text or "/" in text or "\\" in text:
        return None
    if not _JOB_ID_RE.match(text):
        return None
    return text


def sanitize_stage(name: str, stage: dict[str, Any] | None) -> dict[str, Any]:
    st = stage or {}
    status = str(st.get("status") or "PENDING")
    return {
        "name": name,
        "label": STAGE_LABELS_DE.get(name, name),
        "status": status,
        "started_at": st.get("started_at"),
        "finished_at": st.get("finished_at"),
        "error_code": st.get("error_code"),
        "error_message": public_message(st.get("error_message") or "") or None,
        "detail": _public_detail(st.get("detail")),
    }


def _public_detail(detail: Any) -> dict[str, Any] | None:
    if not isinstance(detail, dict):
        return None
    out: dict[str, Any] = {}
    for key, value in detail.items():
        k = str(key)
        if any(m in k.upper() for m in SECRET_MARKERS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str):
                out[k] = public_message(value, limit=160)
            else:
                out[k] = value
        elif isinstance(value, list) and all(isinstance(x, (str, int, float)) for x in value[:20]):
            out[k] = value[:20]
    return out or None


def sanitize_job(job: dict[str, Any]) -> dict[str, Any]:
    stages_in = job.get("stages") if isinstance(job.get("stages"), dict) else {}
    stages = [sanitize_stage(name, stages_in.get(name)) for name in _stage_order(stages_in)]
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    instrument = plan.get("instrument") if isinstance(plan.get("instrument"), dict) else {}
    status = str(job.get("status") or "")
    return {
        "success": True,
        "job_id": job.get("job_id"),
        "symbol": job.get("symbol"),
        "requested_days": job.get("requested_days"),
        "requested_streams": job.get("requested_streams") or [],
        "with_ob1000": bool(job.get("with_ob1000")),
        "restart_live": bool(job.get("restart_live")),
        "apply": bool(job.get("apply")),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "current_stage": job.get("current_stage"),
        "status": status,
        "progress_pct": int(job.get("progress_pct") or 0),
        "error_code": job.get("error_code"),
        "error_message": public_message(job.get("error_message") or "") or None,
        "rollback_status": job.get("rollback_status"),
        "final_verdict": job.get("final_verdict"),
        "history_semantics": job.get("history_semantics") or {},
        "stages": stages,
        "plan": {
            "symbol": plan.get("symbol") or job.get("symbol"),
            "days": plan.get("days") or job.get("requested_days"),
            "with_ob1000": plan.get("with_ob1000", job.get("with_ob1000")),
            "restart_live": plan.get("restart_live", job.get("restart_live")),
            "history_semantics": plan.get("history_semantics") or job.get("history_semantics") or {},
            "instrument": {
                "symbol": instrument.get("symbol"),
                "status": instrument.get("status"),
                "category": instrument.get("category"),
                "quote_coin": instrument.get("quote_coin"),
                "tick_size": instrument.get("tick_size"),
                "qty_step": instrument.get("qty_step"),
                "min_order_qty": instrument.get("min_order_qty"),
                "orderbook_supported": instrument.get("orderbook_supported"),
            },
            "notes": plan.get("notes") or [],
        },
        "active": status in {"RUNNING", "WAITING_FOR_ARCHIVE"},
        "waiting_for_archive_hint": (
            "Das aktuelle Bybit-Tagesarchiv ist noch nicht veröffentlicht. "
            "Die übrigen Datenquellen können bereits funktionieren."
            if status == "WAITING_FOR_ARCHIVE"
            else None
        ),
    }


def _stage_order(stages_in: dict[str, Any]) -> list[str]:
    try:
        from orderbook_analyse.symbol_onboarding.job_contract import STAGE_ORDER

        return list(STAGE_ORDER)
    except Exception:  # noqa: BLE001
        return list(stages_in.keys())


def job_list_row(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job.get("job_id"),
        "symbol": job.get("symbol"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "requested_days": job.get("requested_days"),
        "status": job.get("status"),
        "progress_pct": int(job.get("progress_pct") or 0),
        "final_verdict": job.get("final_verdict"),
        "apply": bool(job.get("apply")),
        "error_code": job.get("error_code"),
    }
