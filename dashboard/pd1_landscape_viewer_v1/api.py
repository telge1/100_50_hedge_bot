"""Routes for PD1 Landscape Viewer under Live Charts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import ALLOWED_DATA_FILES, NAV_ACTIVE, VIEWER_ROOT


def _viewer_file(name: str) -> Path:
    path = (VIEWER_ROOT / name).resolve()
    root = VIEWER_ROOT.resolve()
    if not str(path).startswith(str(root)):
        raise HTTPException(status_code=404, detail="not found")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="viewer asset missing")
    return path


def _window_error(code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"code": code, "message": message, "details": details or {}},
    )


def build_router(*, require_auth: Callable, render_template: Callable) -> APIRouter:
    router = APIRouter()

    @router.get("/live-charts/viewer-poc", response_class=HTMLResponse)
    async def pd1_landscape_viewer_poc_page(request: Request, user: dict = Depends(require_auth)):
        """Isolated POC: TRP chart.js + landscape window API (does not replace /viewer)."""
        return HTMLResponse(
            render_template(
                "pd1_landscape_viewer_poc.html",
                {
                    "request": request,
                    "user": user,
                    "nav_active": NAV_ACTIVE,
                },
            )
        )

    @router.get("/live-charts/viewer", response_class=HTMLResponse)
    async def pd1_landscape_viewer_page(request: Request, user: dict = Depends(require_auth)):
        if not VIEWER_ROOT.is_dir():
            return HTMLResponse(
                render_template(
                    "pd1_landscape_viewer.html",
                    {
                        "request": request,
                        "user": user,
                        "nav_active": NAV_ACTIVE,
                        "viewer_ready": False,
                    },
                ),
                status_code=503,
            )
        return HTMLResponse(
            render_template(
                "pd1_landscape_viewer.html",
                {
                    "request": request,
                    "user": user,
                    "nav_active": NAV_ACTIVE,
                    "viewer_ready": True,
                },
            )
        )

    @router.get("/live-charts/viewer/replay.js")
    async def pd1_viewer_replay_js(user: dict = Depends(require_auth)):
        return FileResponse(_viewer_file("replay.js"), media_type="application/javascript")

    @router.get("/live-charts/viewer/style.css")
    async def pd1_viewer_style_css(user: dict = Depends(require_auth)):
        return FileResponse(_viewer_file("style.css"), media_type="text/css")

    @router.get("/live-charts/viewer/data/{filename}")
    async def pd1_viewer_data(filename: str, user: dict = Depends(require_auth)):
        if filename not in ALLOWED_DATA_FILES:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(_viewer_file(f"data/{filename}"), media_type="application/json")

    @router.get("/live-charts/viewer/api/capabilities")
    async def pd1_viewer_capabilities(user: dict = Depends(require_auth)):
        from research_charts.historical_pool_state_v1 import HistoricalPoolStateError

        from .historical_window_v1 import landscape_capabilities, landscape_window_from_state_err

        try:
            return landscape_capabilities()
        except HistoricalPoolStateError as exc:
            err = landscape_window_from_state_err(exc)
            return _window_error(err.code, err.message, err.details)

    @router.post("/live-charts/viewer/api/window")
    async def pd1_viewer_window(body: dict[str, Any], user: dict = Depends(require_auth)):
        from research_charts.historical_pool_state_v1 import HistoricalPoolStateError

        from .historical_window_v1 import (
            LandscapeWindowError,
            build_landscape_window,
            landscape_window_from_state_err,
        )

        try:
            from_utc = body.get("from_utc") or body.get("from")
            to_utc = body.get("to_utc") or body.get("to")
            if not from_utc or not to_utc:
                raise LandscapeWindowError("INVALID_REQUEST", "from_utc and to_utc required")
            return build_landscape_window(str(from_utc), str(to_utc))
        except LandscapeWindowError as exc:
            return _window_error(exc.code, exc.message, exc.details)
        except HistoricalPoolStateError as exc:
            err = landscape_window_from_state_err(exc)
            return _window_error(err.code, err.message, err.details)

    @router.get("/live-charts/viewer/api/membership")
    async def pd1_viewer_membership(asof_utc: str, user: dict = Depends(require_auth)):
        from .historical_window_v1 import LandscapeWindowError, membership_at_utc

        try:
            return membership_at_utc(asof_utc)
        except LandscapeWindowError as exc:
            return _window_error(exc.code, exc.message, exc.details)

    return router
