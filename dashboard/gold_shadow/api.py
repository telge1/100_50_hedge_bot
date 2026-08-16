"""Gold Shadow HTTP surface. Auth injected from dashboard app. Read-only."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import POLL_INTERVAL_MS, load_gold_shadow_db_config
from .db import configured_executor
from .queries import SelectOnlyExecutor
from .service import (
    build_summary,
    empty_summary,
    list_decisions,
    list_events,
    list_signals,
    list_slots,
    list_trades,
)

_EXECUTOR: SelectOnlyExecutor | None | str = "unset"


def _executor() -> SelectOnlyExecutor | None:
    global _EXECUTOR
    if _EXECUTOR == "unset":
        try:
            _EXECUTOR = configured_executor()
        except Exception:
            _EXECUTOR = None
    return _EXECUTOR if isinstance(_EXECUTOR, SelectOnlyExecutor) else None


def _offline(message: str) -> dict[str, Any]:
    body = empty_summary(connected=False, message=message)
    body["success"] = False
    body["error"] = "gold_shadow_db_unavailable"
    return body


def build_router(
    *,
    require_auth: Callable,
    render_template: Callable,
    executor_factory: Callable[[], SelectOnlyExecutor | None] | None = None,
) -> APIRouter:
    router = APIRouter()
    get_ex = executor_factory or _executor

    @router.get("/gold-shadow")
    async def gold_shadow_legacy_redirect(user: dict = Depends(require_auth)):
        return RedirectResponse(url="/profit-verlauf/gold-shadow", status_code=302)

    @router.get("/profit-verlauf/gold-shadow", response_class=HTMLResponse)
    async def gold_shadow_page(request: Request, user: dict = Depends(require_auth)):
        cfg = None
        try:
            cfg = load_gold_shadow_db_config()
        except Exception:
            cfg = None
        return HTMLResponse(
            render_template(
                "gold_shadow.html",
                {
                    "request": request,
                    "user": user,
                    "poll_interval_ms": POLL_INTERVAL_MS,
                    "db_configured": cfg is not None,
                },
            )
        )

    def _with_ex():
        try:
            return get_ex()
        except Exception:
            return None

    @router.get("/api/gold-shadow/summary")
    async def api_summary(user: dict = Depends(require_auth)):
        ex = _with_ex()
        if ex is None:
            return JSONResponse(_offline("GOLD_SHADOW_DB_* not configured or unreachable"), status_code=503)
        try:
            return build_summary(ex)
        except Exception as exc:
            body = _offline(str(type(exc).__name__))
            return JSONResponse(body, status_code=503)

    @router.get("/api/gold-shadow/slots")
    async def api_slots(user: dict = Depends(require_auth)):
        ex = _with_ex()
        if ex is None:
            return JSONResponse({"success": False, "items": [], "message": "offline"}, status_code=503)
        try:
            return {"success": True, "items": list_slots(ex)}
        except Exception:
            return JSONResponse({"success": False, "items": [], "message": "query_failed"}, status_code=503)

    @router.get("/api/gold-shadow/signals")
    async def api_signals(
        user: dict = Depends(require_auth),
        symbol: str | None = None,
        timeframe: str | None = None,
        direction: str | None = None,
        decision: str | None = None,
        reason: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = Query(default=25),
        offset: int | None = Query(default=0),
    ):
        ex = _with_ex()
        if ex is None:
            return JSONResponse({"success": False, "items": [], "total": 0}, status_code=503)
        try:
            payload = list_signals(
                ex,
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                decision=decision,
                reason=reason,
                start=start,
                end=end,
                limit=limit,
                offset=offset,
            )
            payload["success"] = True
            return payload
        except Exception:
            return JSONResponse({"success": False, "items": [], "total": 0}, status_code=503)

    @router.get("/api/gold-shadow/decisions")
    async def api_decisions(user: dict = Depends(require_auth)):
        ex = _with_ex()
        if ex is None:
            return JSONResponse({"success": False, "counts": {}}, status_code=503)
        try:
            return {"success": True, "counts": list_decisions(ex)}
        except Exception:
            return JSONResponse({"success": False, "counts": {}}, status_code=503)

    @router.get("/api/gold-shadow/trades")
    async def api_trades(
        user: dict = Depends(require_auth),
        limit: int | None = Query(default=25),
        offset: int | None = Query(default=0),
    ):
        ex = _with_ex()
        if ex is None:
            return JSONResponse({"success": False, "items": [], "total": 0}, status_code=503)
        try:
            payload = list_trades(ex, limit=limit, offset=offset)
            payload["success"] = True
            return payload
        except Exception:
            return JSONResponse({"success": False, "items": [], "total": 0}, status_code=503)

    @router.get("/api/gold-shadow/events")
    async def api_events(
        user: dict = Depends(require_auth),
        limit: int | None = Query(default=25),
        offset: int | None = Query(default=0),
    ):
        ex = _with_ex()
        if ex is None:
            return JSONResponse({"success": False, "items": [], "total": 0}, status_code=503)
        try:
            payload = list_events(ex, limit=limit, offset=offset)
            payload["success"] = True
            return payload
        except Exception:
            return JSONResponse({"success": False, "items": [], "total": 0}, status_code=503)

    return router
