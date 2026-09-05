"""FastAPI router: collector health + gated OI backfill jobs."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from .csrf import COOKIE_NAME, csrf_issue_payload, mutate_post_guard
from .jobs import get_job, start_job
from .service import build_health_report, get_collector


def build_router(*, require_auth: Callable) -> APIRouter:
    router = APIRouter(tags=["collector-health"])

    @router.get("/api/collector-health/csrf")
    async def api_csrf(response: Response, user: dict = Depends(require_auth)):
        payload = csrf_issue_payload()
        response.set_cookie(
            key=COOKIE_NAME,
            value=payload["csrf_token"],
            httponly=False,
            samesite="strict",
            max_age=int(payload["ttl_s"]),
        )
        return payload

    @router.get("/api/collector-health")
    async def api_health(user: dict = Depends(require_auth)):
        payload = await asyncio.to_thread(build_health_report)
        return JSONResponse(payload)

    @router.get("/api/collector-health/{collector_id}")
    async def api_health_one(collector_id: str, user: dict = Depends(require_auth)):
        row = await asyncio.to_thread(get_collector, collector_id)
        if row is None:
            return JSONResponse({"error": "UNKNOWN_COLLECTOR_ID"}, status_code=404)
        return JSONResponse(row)

    @router.post("/api/collector-backfill/detect")
    async def api_detect(request: Request, user: dict = Depends(require_auth)):
        return await _mutate(request, user, default_kind="oi_5m_detect")

    @router.post("/api/collector-backfill/start")
    async def api_start(request: Request, user: dict = Depends(require_auth)):
        # Fail-closed: UI start is dry-run unless COLLECTOR_HEALTH_ALLOW_OI_EXECUTE=1
        return await _mutate(request, user, default_kind="oi_5m_backfill_dry_run")

    @router.get("/api/collector-backfill/jobs/{job_id}")
    async def api_job(job_id: str, user: dict = Depends(require_auth)):
        job = await asyncio.to_thread(get_job, job_id)
        if job is None:
            return JSONResponse({"error": "JOB_NOT_FOUND"}, status_code=404)
        return JSONResponse(job)

    async def _mutate(request: Request, user: dict, *, default_kind: str) -> JSONResponse:
        err = mutate_post_guard(
            origin=request.headers.get("origin"),
            referer=request.headers.get("referer"),
            content_type=request.headers.get("content-type"),
            csrf_header=request.headers.get("x-csrf-token"),
            csrf_cookie=request.cookies.get(COOKIE_NAME),
        )
        if err:
            return JSONResponse({"success": False, "error": err}, status_code=403)
        try:
            raw = await request.json()
        except Exception:
            return JSONResponse({"success": False, "error": "JSON_CONTENT_TYPE_REQUIRED"}, status_code=400)
        if not isinstance(raw, dict):
            return JSONResponse({"success": False, "error": "INVALID_BODY"}, status_code=400)
        body: dict[str, Any] = dict(raw)
        body.setdefault("job_kind", default_kind)
        username = str((user or {}).get("username") or (user or {}).get("user") or "unknown")
        payload, status = await asyncio.to_thread(start_job, body=body, user=username)
        return JSONResponse(payload, status_code=status)

    return router
