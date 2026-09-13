"""Plan cache + plan_hash binding (prevents apply with stale/changed form)."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PLAN_TTL_SECONDS, plans_dir
from .schemas import OnboardForm


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_request(form: OnboardForm) -> dict[str, Any]:
    return {
        "symbol": form.symbol,
        "days": form.days,
        "candles_1m": form.candles_1m,
        "open_interest_5m": form.open_interest_5m,
        "public_trades": form.public_trades,
        "with_ob1000": form.with_ob1000,
        "restart_live": form.restart_live,
        "purpose": form.purpose,
    }


def compute_plan_hash(form: OnboardForm, instrument: dict[str, Any]) -> str:
    payload = {
        "request": canonical_request(form),
        "instrument": {
            "symbol": instrument.get("symbol"),
            "status": instrument.get("status"),
            "category": instrument.get("category"),
            "quote_coin": instrument.get("quote_coin"),
            "tick_size": instrument.get("tick_size"),
            "qty_step": instrument.get("qty_step"),
            "min_order_qty": instrument.get("min_order_qty"),
        },
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def store_plan(
    *,
    form: OnboardForm,
    plan_hash: str,
    plan_job: dict[str, Any],
    principal: str,
    environ: dict | None = None,
) -> dict[str, Any]:
    plan_id = f"plan_{uuid.uuid4().hex}"
    now = time.time()
    record = {
        "plan_id": plan_id,
        "plan_hash": plan_hash,
        "created_at": _utc_iso(),
        "expires_at_epoch": now + PLAN_TTL_SECONDS,
        "principal": principal,
        "request": canonical_request(form),
        "plan_job_id": plan_job.get("job_id"),
        "plan": plan_job.get("plan") or {},
        "status": plan_job.get("status"),
        "final_verdict": plan_job.get("final_verdict"),
    }
    path = plans_dir(environ) / f"{plan_id}.json"
    _write_json(path, record)
    return record


def load_plan(plan_id: str, *, environ: dict | None = None) -> dict[str, Any] | None:
    text = str(plan_id or "").strip()
    if not text or ".." in text or "/" in text or "\\" in text:
        return None
    if not text.startswith("plan_") or len(text) > 80:
        return None
    path = plans_dir(environ) / f"{text}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    if float(data.get("expires_at_epoch") or 0) < time.time():
        return None
    return data


def plan_matches_form(record: dict[str, Any], form: OnboardForm, plan_hash: str) -> bool:
    if str(record.get("plan_hash") or "") != str(plan_hash or ""):
        return False
    return record.get("request") == canonical_request(form)
