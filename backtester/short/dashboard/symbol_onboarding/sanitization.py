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
    detail = _public_detail(st.get("detail"))
    return {
        "name": name,
        "label": STAGE_LABELS_DE.get(name, name),
        "status": status,
        "started_at": st.get("started_at"),
        "finished_at": st.get("finished_at"),
        "error_code": st.get("error_code"),
        "error_message": public_message(st.get("error_message") or "") or None,
        "detail": detail,
        "detail_summary": _detail_summary(name, status, detail, st.get("error_message")),
    }


def _detail_summary(
    name: str,
    status: str,
    detail: dict[str, Any] | None,
    error_message: Any,
) -> str | None:
    if error_message and str(status).upper() == "FAILED":
        return public_message(str(error_message), limit=120)
    if not detail:
        return None
    st = str(status).upper()
    if name.startswith("backfill_") and st == "RUNNING":
        return "Backfill läuft…"
    if name.startswith("backfill_") and st == "SUCCEEDED":
        return "Backfill abgeschlossen"
    if name == "register_ob1000" and st == "SUCCEEDED":
        action = detail.get("action")
        if action == "appended":
            return "OB1000 Symbolliste ergänzt (nur live)"
        if action == "already":
            return "bereits in OB1000-Liste"
    if name == "register_universe" and st == "SUCCEEDED":
        if detail.get("already"):
            return "bereits im Universe"
        if detail.get("action") == "appended":
            return "Universe ergänzt"
    reason = detail.get("reason")
    if st == "SKIPPED" and reason:
        if reason == "restart_live_false":
            return "übersprungen (kein Live-Restart)"
        return public_message(str(reason), limit=80)
    return None


def status_headline(job: dict[str, Any], *, stages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Human-facing status for the progress card."""
    status = str(job.get("status") or "").upper()
    symbol = str(job.get("symbol") or "Symbol")
    pct = int(job.get("progress_pct") or 0)
    current = str(job.get("current_stage") or "")
    stage_label = STAGE_LABELS_DE.get(current, current) if current else ""
    if not stage_label and stages:
        for st in stages:
            if str(st.get("status") or "").upper() == "RUNNING":
                stage_label = str(st.get("label") or st.get("name") or "")
                current = str(st.get("name") or "")
                break

    if status in {"QUEUED", "CLAIMED"}:
        return {
            "tone": "run",
            "title": f"{symbol}: in der Warteschlange",
            "subtitle": "Worker übernimmt gleich — Fortschritt erscheint in wenigen Sekunden.",
            "done": False,
        }
    if status == "RUNNING":
        step = f" — {stage_label}" if stage_label else ""
        return {
            "tone": "run",
            "title": f"{symbol}: läuft{step}",
            "subtitle": f"Fortschritt {pct}%. Bitte Seite offen lassen — Status aktualisiert sich automatisch.",
            "done": False,
        }
    if status == "WAITING_FOR_ARCHIVE":
        return {
            "tone": "wait",
            "title": f"{symbol}: wartet auf Bybit-Tagesarchiv",
            "subtitle": "Übrige Quellen können schon nutzbar sein. Job läuft weiter, wenn das Archiv da ist.",
            "done": False,
        }
    if status == "SUCCEEDED":
        return {
            "tone": "ok",
            "title": f"{symbol}: fertig eingerichtet",
            "subtitle": "Historie und Registrierung abgeschlossen. Symbol ist bereit.",
            "done": True,
        }
    if status == "PARTIAL_LIVE_ONLY":
        return {
            "tone": "ok",
            "title": f"{symbol}: Historie fertig (ohne Live-Restart)",
            "subtitle": (
                "Candles/Trades und Registrierung sind durch. "
                "OB1000/Live starten erst mit „live aktivieren“ beim nächsten Onboarding."
            ),
            "done": True,
        }
    if status == "PLANNED":
        return {
            "tone": "wait",
            "title": f"{symbol}: Vorprüfung ok",
            "subtitle": "Noch nicht angewendet — „Symbol hinzufügen“ startet den echten Job.",
            "done": False,
        }
    if status in {"FAILED", "ROLLED_BACK"}:
        err = public_message(str(job.get("error_message") or job.get("error_code") or "fehlgeschlagen"))
        return {
            "tone": "fail",
            "title": f"{symbol}: fehlgeschlagen",
            "subtitle": err,
            "done": True,
        }
    if status == "COMPLETED":
        return {
            "tone": "ok",
            "title": f"{symbol}: fertig",
            "subtitle": str(job.get("final_verdict") or "Job abgeschlossen"),
            "done": True,
        }
    return {
        "tone": "wait",
        "title": f"{symbol}: {status or 'unbekannt'}",
        "subtitle": str(job.get("final_verdict") or job.get("message") or ""),
        "done": status not in {"RUNNING", "WAITING_FOR_ARCHIVE", "QUEUED", "CLAIMED"},
    }


def sanitize_queue_job(job_id: str, queue_status: dict[str, Any]) -> dict[str, Any]:
    """Fallback payload while OA job JSON is not written yet (or queue-only failure)."""
    state = str(queue_status.get("state") or "QUEUED").upper()
    symbol = str(job_id).split("_", 1)[0] if job_id else "Symbol"
    mapped = {
        "QUEUED": "QUEUED",
        "CLAIMED": "CLAIMED",
        "RUNNING": "RUNNING",
        "FAILED": "FAILED",
        "COMPLETED": "SUCCEEDED",
        "RECOVERY_REQUIRED": "FAILED",
    }.get(state, state)
    fake = {
        "job_id": job_id,
        "symbol": symbol,
        "status": mapped,
        "progress_pct": 5 if mapped in {"QUEUED", "CLAIMED"} else int(queue_status.get("progress_pct") or 0),
        "created_at": queue_status.get("started_at") or queue_status.get("claimed_at"),
        "updated_at": queue_status.get("finished_at") or queue_status.get("started_at"),
        "error_code": queue_status.get("error_code"),
        "error_message": queue_status.get("message"),
        "final_verdict": queue_status.get("message"),
        "stages": {},
        "current_stage": None,
        "queue_state": state,
        "queue_message": queue_status.get("message"),
    }
    public = sanitize_job(fake)
    public["queue_only"] = True
    public["queue_state"] = state
    public["queue_message"] = public_message(str(queue_status.get("message") or "")) or None
    return public


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
    headline = status_headline(job, stages=stages)
    done_count = sum(1 for s in stages if str(s.get("status") or "").upper() in {"SUCCEEDED", "SKIPPED"})
    total_count = len(stages) or 0
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
        "current_stage_label": STAGE_LABELS_DE.get(
            str(job.get("current_stage") or ""), str(job.get("current_stage") or "")
        )
        or next(
            (str(s.get("label")) for s in stages if str(s.get("status") or "").upper() == "RUNNING"),
            "",
        ),
        "status": status,
        "progress_pct": int(job.get("progress_pct") or 0),
        "stages_done": done_count,
        "stages_total": total_count,
        "headline": headline,
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
        "active": status in {"RUNNING", "WAITING_FOR_ARCHIVE", "QUEUED", "CLAIMED"},
        "done": bool(headline.get("done")),
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
