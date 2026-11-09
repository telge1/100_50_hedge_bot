"""Shadow signal HTTP surface. Auth injected from dashboard app. Read-only."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from shadow_signals.db import configured_executor
from shadow_signals.queries import SelectOnlyExecutor, normalize_side
from shadow_signals.service import build_offline_payload, build_shadow_signals_payload

_EXECUTOR: SelectOnlyExecutor | None | str = "unset"
OFFLINE_MESSAGE = "Shadow-Signal-Daten aktuell nicht verfügbar"


def _executor() -> SelectOnlyExecutor | None:
    global _EXECUTOR
    if _EXECUTOR == "unset":
        try:
            _EXECUTOR = configured_executor()
        except Exception:
            _EXECUTOR = None
    return _EXECUTOR if isinstance(_EXECUTOR, SelectOnlyExecutor) else None


def build_router(
    *,
    require_auth: Callable,
    render_template: Callable,
    executor_factory: Callable[[], SelectOnlyExecutor | None] | None = None,
) -> APIRouter:
    router = APIRouter()
    get_ex = executor_factory or _executor

    def _with_ex() -> SelectOnlyExecutor | None:
        try:
            return get_ex()
        except Exception:
            return None

    def _query_params(
        side: str | None,
        start_time: str | None,
        end_time: str | None,
        page: int | None,
        page_size: int | None,
    ) -> dict[str, Any]:
        return {
            "side": normalize_side(side),
            "start_time": start_time,
            "end_time": end_time,
            "page": page,
            "page_size": page_size,
        }

    @router.get("/profit-verlauf/shadow-signals", response_class=HTMLResponse)
    async def shadow_signals_page(
        request: Request,
        side: str = Query("long", description="Signal side (long|short)"),
        start_time: str | None = Query(None, description="Filter start (dashboard TZ)"),
        end_time: str | None = Query(None, description="Filter end (dashboard TZ)"),
        page: int = Query(0, ge=0),
        page_size: int | None = Query(50, ge=1, le=100),
        user: dict = Depends(require_auth),
    ):
        ex = _with_ex()
        params = _query_params(side, start_time, end_time, page, page_size)
        if ex is None:
            payload = build_offline_payload(**params, message=OFFLINE_MESSAGE)
            ch_available = False
        else:
            try:
                payload = build_shadow_signals_payload(ex, **params)
                ch_available = True
            except Exception:
                payload = build_offline_payload(**params, message=OFFLINE_MESSAGE)
                ch_available = False
        return HTMLResponse(
            render_template(
                "shadow_signals.html",
                {
                    "request": request,
                    "user": user,
                    "side": payload["side"],
                    "rows": payload["rows"],
                    "summary": payload["summary"],
                    "pagination": payload["pagination"],
                    "filter_start_time": payload["filters"]["start_time_input"],
                    "filter_end_time": payload["filters"]["end_time_input"],
                    "page_size": payload["filters"]["page_size"],
                    "trade_page": payload["pagination"]["page"],
                    "offline": not ch_available or payload.get("offline"),
                    "offline_message": payload.get("message", OFFLINE_MESSAGE),
                    "columns": payload["columns"],
                },
            )
        )

    @router.get("/api/dashboard/shadow-signals")
    async def api_shadow_signals(
        side: str = Query("long", description="Signal side (long|short)"),
        start_time: str | None = Query(None),
        end_time: str | None = Query(None),
        page: int = Query(0, ge=0),
        page_size: int | None = Query(50, ge=1, le=100),
        user: dict = Depends(require_auth),
    ):
        ex = _with_ex()
        params = _query_params(side, start_time, end_time, page, page_size)
        if ex is None:
            body = build_offline_payload(**params, message=OFFLINE_MESSAGE)
            return JSONResponse(body, status_code=503)
        try:
            body = build_shadow_signals_payload(ex, **params)
            return body
        except Exception:
            body = build_offline_payload(**params, message=OFFLINE_MESSAGE)
            return JSONResponse(body, status_code=503)

    return router
