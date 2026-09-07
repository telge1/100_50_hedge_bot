"""HTTP routes for footprint candles (read-only)."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from . import ASSET_V, FORMAT_VERSION
from .contracts import (
    BUCKET_STEP,
    DEFAULT_BUFFER_SECONDS,
    IMBALANCE_RATIO,
    MAX_CANDLES,
    MAX_CLOSED_CANDLES,
    MAX_RANGE_SECONDS,
    MIN_COMPARE_SIZE,
    STACKED_MIN_LEVELS,
    SUPPORTED_MODE,
    SUPPORTED_SYMBOL,
    SUPPORTED_TIMEFRAME,
)
from .response_contracts import (
    DEFAULT_THRESHOLDS,
    RESPONSE_ENGINE_ID,
    RESPONSE_ENGINE_VERSION,
    config_hash,
)
from .service import FootprintRequestError, load_footprint


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"success": False, "error": code, "message": message},
    )


def build_router(*, require_auth: Callable) -> APIRouter:
    router = APIRouter()

    @router.get("/api/footprint-candles/meta")
    async def api_footprint_meta(user: dict = Depends(require_auth)):
        return {
            "success": True,
            "format_version": FORMAT_VERSION,
            "asset_v": ASSET_V,
            "supported": {
                "symbol": SUPPORTED_SYMBOL,
                "timeframe": SUPPORTED_TIMEFRAME,
                "mode": SUPPORTED_MODE,
                "bucket_step": float(BUCKET_STEP),
            },
            "imbalance": {
                "ratio": IMBALANCE_RATIO,
                "min_compare_size": MIN_COMPARE_SIZE,
                "stacked_min_levels": STACKED_MIN_LEVELS,
                "unit": "size",
            },
            "limits": {
                "max_range_seconds": MAX_RANGE_SECONDS,
                "max_closed_candles_5m": MAX_CLOSED_CANDLES,
                "max_candles": MAX_CANDLES,
                "default_buffer_seconds": DEFAULT_BUFFER_SECONDS,
                "note": (
                    "Hard span is 6h (=72 closed 5m intervals). "
                    "max_candles=73 allows one forming candle. "
                    "Client ±buffer is clamped into the 6h span."
                ),
            },
            "display_volume": "notional",
            "ohlc_source": "signal_generator.candles_1m",
            "trades_source": "orderbook_analysis.public_trades_canonical",
            "avr_engine": {
                "id": RESPONSE_ENGINE_ID,
                "version": RESPONSE_ENGINE_VERSION,
                "config_hash": config_hash(DEFAULT_THRESHOLDS),
                "thresholds_provisional": True,
                "embedded_in_footprint_response": True,
                "query_param": "avr=0|1 (default 1)",
                "note": "Decision support only — not a trading signal.",
            },
        }

    @router.get("/api/footprint-candles")
    async def api_footprint(
        user: dict = Depends(require_auth),
        symbol: str = Query(...),
        timeframe: str = Query(SUPPORTED_TIMEFRAME),
        mode: str = Query(SUPPORTED_MODE),
        bucket_step: float = Query(float(BUCKET_STEP)),
        start: int = Query(..., alias="from", description="UTC unix seconds inclusive"),
        end: int = Query(..., alias="to", description="UTC unix seconds exclusive"),
        avr: int = Query(1, description="1=embed AVR summaries, 0=skip"),
    ) -> Any:
        try:
            payload = await asyncio.to_thread(
                load_footprint,
                symbol=symbol,
                timeframe=timeframe,
                mode=mode,
                bucket_step=bucket_step,
                start=start,
                end=end,
                include_avr=bool(int(avr)),
            )
        except FootprintRequestError as exc:
            status = 400
            if exc.code in {"query_failed"}:
                status = 502
            return _error(status, exc.code, exc.message)
        except Exception:  # noqa: BLE001
            return _error(502, "query_failed", "Footprint query failed")
        return payload

    return router
