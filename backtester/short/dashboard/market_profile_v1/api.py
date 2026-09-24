"""Routes for the anchored market profile page. Read-only."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import ASSET_V, NAV_ACTIVE
from .service import (
    DEFAULT_TARGET_BINS,
    DEFAULT_VALUE_AREA_PCT,
    MAX_RANGE_DAYS,
    MAX_WINDOWS,
    SHAPE_NOTICE,
    SUPPORTED_ANCHORS,
    SUPPORTED_MP_TIMEFRAMES,
    SUPPORTED_TIMEFRAMES,
    ProfileRequestError,
    known_symbols,
    load_profiles,
    session_names,
)
from .dual_profile import DUAL_CONTRACT_VERSION


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"success": False, "error": code, "message": message},
    )


def build_router(*, require_auth: Callable, render_template: Callable) -> APIRouter:
    router = APIRouter()

    @router.get("/live-charts/market-profile", response_class=HTMLResponse)
    async def market_profile_page(
        request: Request, user: dict = Depends(require_auth)
    ):
        return HTMLResponse(
            render_template(
                "market_profile_v1.html",
                {
                    "request": request,
                    "user": user,
                    "nav_active": NAV_ACTIVE,
                    "asset_v": ASSET_V,
                    "shape_notice": SHAPE_NOTICE,
                    "symbols": known_symbols(),
                    "sessions": session_names(),
                    "timeframes": list(SUPPORTED_TIMEFRAMES),
                    "mp_timeframes": list(SUPPORTED_MP_TIMEFRAMES),
                    "anchors": list(SUPPORTED_ANCHORS),
                    "max_windows": MAX_WINDOWS,
                    "max_range_days": MAX_RANGE_DAYS,
                },
            )
        )

    @router.get("/api/market-profile/meta")
    async def api_market_profile_meta(user: dict = Depends(require_auth)):
        return {
            "success": True,
            "symbols": known_symbols(),
            "sessions": session_names(),
            "timeframes": list(SUPPORTED_TIMEFRAMES),
            "mp_timeframes": list(SUPPORTED_MP_TIMEFRAMES),
            "anchors": list(SUPPORTED_ANCHORS),
            "defaults": {
                "value_area_pct": DEFAULT_VALUE_AREA_PCT,
                "target_bins": DEFAULT_TARGET_BINS,
                "timeframe": "15m",
                "mp_timeframe": "day",
            },
            "limits": {"max_windows": MAX_WINDOWS, "max_range_days": MAX_RANGE_DAYS},
            "shape_unvalidated": True,
            "shape_notice": SHAPE_NOTICE,
            "dual_contract_version": DUAL_CONTRACT_VERSION,
            "tpo_contract": "tpo_profile_facts_v1",
            "volume_contract": "volume_profile_facts_v1",
            "profile_timeframe_independent": True,
        }

    @router.get("/api/market-profile/profiles")
    async def api_market_profile_profiles(
        user: dict = Depends(require_auth),
        symbol: str = Query(...),
        start: int = Query(..., description="UTC unix seconds, inclusive"),
        end: int = Query(..., description="UTC unix seconds, exclusive"),
        mp_timeframe: str | None = Query(
            None,
            description=(
                "Profile window type: 5m/15m/30m/1h/4h/day/session/composite. "
                "Independent of candle timeframe."
            ),
        ),
        anchor: str | None = Query(
            None,
            description="Legacy alias for mp_timeframe (day/session/composite/periods)",
        ),
        sessions: str | None = Query(None, description="csv, session mp_timeframe only"),
        timeframe: str = Query("15m", description="candle timeframe for the chart only"),
        value_area_pct: float = Query(DEFAULT_VALUE_AREA_PCT),
        target_bins: int = Query(DEFAULT_TARGET_BINS),
        final: int = Query(0, description="1 = FINAL dedupe on the trade scan"),
        include_bins: int = Query(1),
    ) -> Any:
        try:
            payload = await asyncio.to_thread(
                load_profiles,
                symbol=symbol,
                start=start,
                end=end,
                mp_timeframe=mp_timeframe,
                anchor=anchor or "day",
                sessions=sessions,
                timeframe=timeframe,
                value_area_pct=value_area_pct,
                target_bins=target_bins,
                use_final=bool(int(final)),
                include_bins=bool(int(include_bins)),
            )
        except ProfileRequestError as exc:
            status = 404 if exc.code in ("no_candles", "no_profiles", "no_windows") else 400
            return _error(status, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - surface as JSON, never a 500 page
            return _error(502, "upstream_failed", f"{type(exc).__name__}: {exc}")
        return payload

    return router
