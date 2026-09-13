"""FastAPI routes for /datenverwaltung/symbole — thin adapter only."""

from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .auth import auth_roles_for_onboard, is_admin, require_admin
from .config import MAX_REQUEST_BYTES, STAGE_LABELS_DE
from .origin import post_guard
from .overview import build_overview
from .plans import compute_plan_hash, load_plan, plan_matches_form, store_plan
from .rate_limit import allow_post
from .sanitization import job_list_row, sanitize_job, validate_job_id
from .schemas import parse_onboard_form
from .service_adapter import get_job, list_job_files, run_plan
from .worker_client import enqueue_apply


async def _read_json_body(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        return None, JSONResponse({"success": False, "error": "REQUEST_TOO_LARGE"}, status_code=413)
    if not body:
        return None, JSONResponse({"success": False, "error": "EMPTY_BODY"}, status_code=400)
    try:
        raw = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None, JSONResponse({"success": False, "error": "INVALID_JSON"}, status_code=400)
    if not isinstance(raw, dict):
        return None, JSONResponse({"success": False, "error": "INVALID_JSON"}, status_code=400)
    return raw, None


def build_router(*, require_auth: Callable, render_template: Callable) -> APIRouter:
    router = APIRouter(tags=["symbol-onboarding"])

    def _admin_user(user: dict = Depends(require_auth)) -> dict:
        return require_admin(user)

    @router.get("/datenverwaltung/symbole", response_class=HTMLResponse)
    async def page_symbol_onboarding(request: Request, user: dict = Depends(require_auth)):
        require_admin(user)
        html = render_template(
            "symbol_onboarding.html",
            {
                "request": request,
                "user": user,
                "stage_labels": STAGE_LABELS_DE,
                "is_admin": True,
            },
        )
        return HTMLResponse(html)

    @router.post("/api/datenverwaltung/symbole/plan")
    async def api_plan(request: Request, user: dict = Depends(_admin_user)):
        guard = post_guard(
            origin=request.headers.get("origin"),
            referer=request.headers.get("referer"),
            content_type=request.headers.get("content-type"),
        )
        if guard:
            return JSONResponse({"success": False, "error": guard}, status_code=403)
        if not allow_post(str(user.get("username") or "admin")):
            return JSONResponse({"success": False, "error": "RATE_LIMITED"}, status_code=429)

        raw, err_resp = await _read_json_body(request)
        if err_resp:
            return err_resp
        assert raw is not None
        form, ferr = parse_onboard_form(raw)
        if ferr or form is None:
            return JSONResponse({"success": False, "error": ferr or "INVALID_FORM"}, status_code=400)

        roles = auth_roles_for_onboard(user)
        try:
            job = run_plan(
                form,
                principal=str(user.get("username") or "admin"),
                roles=roles,
            )
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", None) or "PLAN_FAILED"
            msg = getattr(exc, "args", [None])[0] if getattr(exc, "args", None) else str(exc)
            from .sanitization import public_message

            return JSONResponse(
                {
                    "success": False,
                    "error": code,
                    "error_message": public_message(str(msg)),
                },
                status_code=400,
            )

        instrument = (job.get("plan") or {}).get("instrument") or {}
        plan_hash = compute_plan_hash(form, instrument if isinstance(instrument, dict) else {})
        record = store_plan(
            form=form,
            plan_hash=plan_hash,
            plan_job=job,
            principal=str(user.get("username") or "admin"),
        )
        public = sanitize_job(job)
        return JSONResponse(
            {
                "success": True,
                "plan_id": record["plan_id"],
                "plan_hash": plan_hash,
                "request": form.to_public(),
                "job": public,
                "warnings": (job.get("plan") or {}).get("notes") or [],
                "collector_restarts": bool(form.restart_live),
            }
        )

    @router.post("/api/datenverwaltung/symbole/apply")
    async def api_apply(request: Request, user: dict = Depends(_admin_user)):
        guard = post_guard(
            origin=request.headers.get("origin"),
            referer=request.headers.get("referer"),
            content_type=request.headers.get("content-type"),
        )
        if guard:
            return JSONResponse({"success": False, "error": guard}, status_code=403)
        if not allow_post(str(user.get("username") or "admin")):
            return JSONResponse({"success": False, "error": "RATE_LIMITED"}, status_code=429)

        raw, err_resp = await _read_json_body(request)
        if err_resp:
            return err_resp
        assert raw is not None

        if not bool(raw.get("confirm")):
            return JSONResponse({"success": False, "error": "CONFIRMATION_REQUIRED"}, status_code=400)

        plan_id = str(raw.get("plan_id") or "").strip()
        plan_hash = str(raw.get("plan_hash") or "").strip()
        if not plan_id or not plan_hash:
            return JSONResponse({"success": False, "error": "PLAN_REQUIRED"}, status_code=400)

        form, ferr = parse_onboard_form(raw)
        if ferr or form is None:
            return JSONResponse({"success": False, "error": ferr or "INVALID_FORM"}, status_code=400)

        record = load_plan(plan_id)
        if not record:
            return JSONResponse({"success": False, "error": "PLAN_EXPIRED_OR_UNKNOWN"}, status_code=400)
        if not plan_matches_form(record, form, plan_hash):
            return JSONResponse({"success": False, "error": "PLAN_HASH_MISMATCH"}, status_code=409)

        payload, status = enqueue_apply(
            form=form,
            plan_id=plan_id,
            plan_hash=plan_hash,
            principal=str(user.get("username") or "admin"),
        )
        return JSONResponse(payload, status_code=status)

    @router.get("/api/datenverwaltung/symbole/jobs")
    async def api_jobs(
        user: dict = Depends(_admin_user),
        limit: int = 20,
        offset: int = 0,
    ):
        limit = max(1, min(int(limit or 20), 100))
        offset = max(0, int(offset or 0))
        files = list_job_files()
        # Prefer apply jobs; still include recent plans if needed
        rows = []
        for path in files:
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(job, dict):
                continue
            rows.append(job_list_row(job))
        page = rows[offset : offset + limit]
        return JSONResponse(
            {
                "success": True,
                "total": len(rows),
                "limit": limit,
                "offset": offset,
                "jobs": page,
            }
        )

    @router.get("/api/datenverwaltung/symbole/jobs/{job_id}")
    async def api_job(job_id: str, user: dict = Depends(_admin_user)):
        safe = validate_job_id(job_id)
        if not safe:
            return JSONResponse({"success": False, "error": "INVALID_JOB_ID"}, status_code=400)
        try:
            job = get_job(safe)
        except FileNotFoundError:
            return JSONResponse({"success": False, "error": "JOB_NOT_FOUND"}, status_code=404)
        return JSONResponse(sanitize_job(job))

    @router.get("/api/datenverwaltung/symbole/overview")
    async def api_overview(user: dict = Depends(_admin_user)):
        return JSONResponse(build_overview())

    @router.get("/api/datenverwaltung/symbole/meta")
    async def api_meta(user: dict = Depends(_admin_user)):
        return JSONResponse(
            {
                "success": True,
                "is_admin": is_admin(user),
                "stage_labels": STAGE_LABELS_DE,
                "max_history_days": 90,
                "presets": [7, 30, 90],
                "forbidden": ["full_ob", "orderbook.full"],
            }
        )

    return router
