"""Research Charts HTTP surface. Auth injected from dashboard app."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .boundary import (
    INDICATOR_CONTRACTS,
    STREAM_CONTRACT,
    SUPPORTED_LAYOUTS,
    SUPPORTED_TIMEFRAMES,
    TRP_ROOT,
)
from .clickhouse_source import SOURCE_NAME
from .collector_control import POLL_INTERVAL_MS, fetch_forming_candle, live_status_for_symbol
from .service import (
    DEFAULT_PANE_TIMEFRAMES,
    candle_objects,
    candle_source_name,
    compute_indicators,
    default_limit,
    list_symbols,
    load_candles,
    pane_bundle,
    symbol_meta,
)
from .workspace_session import get_workspace

FEED_MESSAGE = (
    "ClickHouse signal_generator.candles_1m · Live1mCollector "
    "(History + Live-Kerze + Forming-Preis)."
)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"success": False, "error": code, "message": message, "feed_ready": True},
        status_code=status,
    )


def build_router(*, require_auth: Callable, render_template: Callable) -> APIRouter:
    router = APIRouter()

    @router.get("/live-charts/research", response_class=HTMLResponse)
    async def research_charts_page(request: Request, user: dict = Depends(require_auth)):
        return HTMLResponse(
            render_template(
                "research_charts.html",
                {
                    "request": request,
                    "user": user,
                    "feed_ready": True,
                    "feed_message": FEED_MESSAGE,
                    "layouts": list(SUPPORTED_LAYOUTS),
                    "timeframes": list(SUPPORTED_TIMEFRAMES),
                    "default_panes": list(DEFAULT_PANE_TIMEFRAMES),
                    "indicators": list(INDICATOR_CONTRACTS),
                    "poll_interval_ms": POLL_INTERVAL_MS,
                },
            )
        )

    @router.get("/api/research/symbols")
    async def api_research_symbols(user: dict = Depends(require_auth)):
        rows = list_symbols()
        return {
            "success": True,
            "feed_ready": True,
            "source": candle_source_name(),
            "message": FEED_MESSAGE,
            "symbols": rows,
            "poll_interval_ms": POLL_INTERVAL_MS,
        }

    @router.get("/api/research/candles")
    async def api_research_candles(
        user: dict = Depends(require_auth),
        symbol: str = Query(...),
        timeframe: str = Query("5m"),
        limit: Optional[int] = Query(None, ge=1, le=3000),
        start: Optional[int] = Query(None, alias="from"),
        end: Optional[int] = Query(None, alias="to"),
    ):
        tf = str(timeframe)
        if tf not in SUPPORTED_TIMEFRAMES:
            return _error(400, "invalid_timeframe", f"unsupported timeframe {tf}")
        try:
            payload = await asyncio.to_thread(
                load_candles,
                symbol,
                tf,
                start=start,
                end=end,
                limit=limit or default_limit(tf),
            )
        except KeyError:
            return _error(404, "unknown_symbol", f"no 1m candles for {symbol}")
        except ValueError as exc:
            code = str(exc)
            return _error(400, code, str(exc))
        except Exception as exc:
            return _error(500, "candle_load_failed", str(exc))
        return {"success": True, "message": FEED_MESSAGE, **payload}

    @router.post("/api/research/pane")
    async def api_research_pane(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        tf = str(body.get("timeframe") or "5m")
        if tf not in SUPPORTED_TIMEFRAMES:
            return _error(400, "invalid_timeframe", f"unsupported timeframe {tf}")
        try:
            payload = await asyncio.to_thread(
                pane_bundle,
                str(body.get("symbol") or ""),
                tf,
                start=body.get("from"),
                end=body.get("to"),
                limit=body.get("limit"),
                ema=body.get("ema"),
                stochastic=body.get("stochastic"),
                liquidity=body.get("liquidity"),
                allow_stale=bool(body.get("allow_stale")),
            )
        except KeyError:
            return _error(404, "unknown_symbol", "no 1m candles for symbol")
        except ValueError as exc:
            return _error(400, str(exc), str(exc))
        except Exception as exc:
            return _error(500, "pane_load_failed", str(exc))
        return {"success": True, "message": FEED_MESSAGE, **payload}

    @router.get("/api/research/live-status")
    async def api_research_live_status(
        user: dict = Depends(require_auth),
        symbol: str = Query(...),
        ensure: bool = Query(False),
    ):
        meta = symbol_meta(symbol)
        history = bool(meta)
        last_closed = int(meta["last_time"]) if meta else None
        payload = await asyncio.to_thread(
            live_status_for_symbol,
            symbol,
            history_available=history,
            last_closed_time=last_closed,
            ensure=ensure,
        )
        payload["success"] = True
        payload["feed_ready"] = True
        payload["source"] = candle_source_name()
        return payload

    @router.get("/api/research/forming-bar")
    async def api_research_forming_bar(
        user: dict = Depends(require_auth),
        symbol: str = Query(...),
    ):
        bar = await asyncio.to_thread(fetch_forming_candle, symbol)
        return {
            "success": True,
            "symbol": str(symbol or "").strip().upper(),
            "forming": bar,
        }

    @router.get("/api/research/indicators")
    async def api_research_indicators_get(
        user: dict = Depends(require_auth),
        symbol: str = Query(...),
        timeframe: str = Query("5m"),
        limit: Optional[int] = Query(None, ge=1, le=3000),
        start: Optional[int] = Query(None, alias="from"),
        end: Optional[int] = Query(None, alias="to"),
        ema: bool = Query(False),
        stochastic: bool = Query(False),
        liquidity: bool = Query(False),
        k_length: int = Query(14),
        k_smoothing: int = Query(3),
        d_smoothing: int = Query(3),
        highest_len: int = Query(2),
        lowest_len: int = Query(2),
        amount: int = Query(300),
    ):
        try:
            payload = await asyncio.to_thread(
                compute_indicators,
                symbol,
                timeframe,
                start=start,
                end=end,
                limit=limit,
                ema={"enabled": ema},
                stochastic={
                    "enabled": stochastic,
                    "k_length": k_length,
                    "k_smoothing": k_smoothing,
                    "d_smoothing": d_smoothing,
                },
                liquidity={
                    "enabled": liquidity,
                    "highest_len": highest_len,
                    "lowest_len": lowest_len,
                    "amount": amount,
                },
            )
        except KeyError:
            return _error(404, "unknown_symbol", f"no 1m candles for {symbol}")
        except ValueError as exc:
            return _error(400, str(exc), str(exc))
        except Exception as exc:
            return _error(500, "indicator_failed", str(exc))
        return payload

    @router.post("/api/research/indicators")
    async def api_research_indicators_post(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        try:
            payload = await asyncio.to_thread(
                compute_indicators,
                str(body.get("symbol") or ""),
                str(body.get("timeframe") or "5m"),
                start=body.get("from"),
                end=body.get("to"),
                limit=body.get("limit"),
                ema=body.get("ema"),
                stochastic=body.get("stochastic"),
                liquidity=body.get("liquidity"),
            )
        except KeyError:
            return _error(404, "unknown_symbol", "no 1m candles for symbol")
        except ValueError as exc:
            return _error(400, str(exc), str(exc))
        except Exception as exc:
            return _error(500, "indicator_failed", str(exc))
        return payload

    @router.get("/api/research/stream")
    async def api_research_stream(user: dict = Depends(require_auth)):
        return {
            "success": True,
            "feed_ready": False,
            **STREAM_CONTRACT,
            "note": (
                "Phase 3 live delivery is incremental candle polling (5s) from "
                "ClickHouse. SSE may replace polling later. No Research Bybit WS."
            ),
            "poll_interval_ms": POLL_INTERVAL_MS,
        }

    @router.get("/api/research/meta")
    async def api_research_meta(user: dict = Depends(require_auth)):
        return {
            "success": True,
            "feed_ready": True,
            "layouts": list(SUPPORTED_LAYOUTS),
            "timeframes": list(SUPPORTED_TIMEFRAMES),
            "default_panes": list(DEFAULT_PANE_TIMEFRAMES),
            "trp_root": str(TRP_ROOT),
            "engine_boundary": "python",
            "source": candle_source_name(),
            "canonical_source": SOURCE_NAME,
            "realtime": True,
            "realtime_mode": "forming_1m_poll",
            "poll_interval_ms": POLL_INTERVAL_MS,
            "forming_poll_ms": 250,
        }

    @router.get("/api/research/workspace")
    async def api_research_workspace(user: dict = Depends(require_auth)):
        return get_workspace().snapshot()

    @router.get("/api/research/settings/defaults")
    async def api_research_settings_defaults(user: dict = Depends(require_auth)):
        return {"success": True, **get_workspace().settings_defaults()}

    @router.put("/api/research/settings")
    async def api_research_settings_put(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        try:
            return get_workspace().apply_settings(
                ema=body.get("ema"),
                stochastic=body.get("stochastic"),
                liquidity=body.get("liquidity"),
            )
        except ValueError as exc:
            return _error(400, "invalid_settings", str(exc))

    @router.post("/api/research/indicator-enabled")
    async def api_research_indicator_enabled(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        try:
            return get_workspace().set_indicator_enabled(
                str(body.get("name") or ""), bool(body.get("enabled"))
            )
        except ValueError as exc:
            return _error(400, "invalid_indicator", str(exc))

    @router.post("/api/research/overlay-test")
    async def api_research_overlay_test(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        return get_workspace().set_overlay_test(
            bool(body.get("enabled")), str(body.get("symbol") or "").upper()
        )

    @router.post("/api/research/backtester/load")
    async def api_research_backtester_load(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        from .stoch_backtester import fetch_stoch_signal_rows

        symbol = str(body.get("symbol") or "").strip().upper()
        if not symbol:
            return _error(400, "symbol_required", "symbol required")
        hours = int(body.get("hours") or 48)
        strategy_version = str(body.get("strategy_version") or "").strip() or None
        source = str(body.get("source") or "").strip() or None
        job_id = str(body.get("job_id") or "").strip() or None
        out = await asyncio.to_thread(
            fetch_stoch_signal_rows,
            symbol=symbol,
            hours=hours,
            strategy_version=strategy_version,
            source=source,
            job_id=job_id,
        )
        rows, err = out[0], out[1]
        job_meta = out[2] if len(out) > 2 else {}
        if err:
            code = 400 if err in (
                "FROZEN_STRATEGY_REQUIRES_RESEARCH_JOB",
                "JOB_NOT_SELECTABLE",
                "JOB_ID_INVALID",
                "JOB_NOT_FOUND",
                "JOB_ARTIFACT_INVALID",
                "FROZEN_IDENTITY_MISMATCH",
            ) else 503
            return _error(code, "signal_feed_unavailable", err)
        snap = get_workspace().import_stoch_backtester(symbol, rows)
        bt = dict(snap.get("backtester") or {})
        if source == "FROZEN_RESEARCH_JOB" or job_id:
            first = rows[0] if rows else {}
            job = job_meta if isinstance(job_meta, dict) else {}
            bt["source"] = "FROZEN_RESEARCH_JOB"
            bt["job_id"] = job_id or job.get("job_id")
            bt["strategy_version"] = str(
                job.get("strategy_version")
                or first.get("strategy_version")
                or "wave_fade_frozen_f16ae32"
            )
            bt["outcomes_computed"] = False
            bt["display_mode"] = "PLANNED_NO_OUTCOME"
            bt["signal_start"] = job.get("signal_start") or first.get("job_signal_start")
            bt["signal_end_exclusive"] = job.get("signal_end_exclusive") or first.get(
                "job_signal_end_exclusive"
            )
            bt["plan_horizon"] = "4h visual projection only"
            bt["message"] = (
                "PLANNED_NO_OUTCOME · Entry/TP/SL · Outcomes nicht berechnet · "
                "4h-Planhorizont nur visuelle Projektion"
            )
        else:
            bt["strategy_version"] = strategy_version or "wave_fade_no_be50_v1"
        if strategy_version == "POOL_ORDER_PLAN_V1" and not rows:
            bt["message"] = f"Pool-V1 hat keine Signale für {symbol}"
        if strategy_version == "EMA_POOL_TREND_FLIP_V1" and not rows:
            bt["message"] = f"EMA Pool Trend Flip V1 hat keine Signale für {symbol}"
        snap["backtester"] = bt
        return snap

    @router.post("/api/research/drawings/tool")
    async def api_research_drawings_tool(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        return get_workspace().set_drawing_tool(str(body.get("tool") or "select"))

    @router.post("/api/research/drawings/event")
    async def api_research_drawings_event(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        return get_workspace().handle_event(body)

    @router.post("/api/research/drawings/delete")
    async def api_research_drawings_delete(user: dict = Depends(require_auth)):
        return get_workspace().delete_selected()

    @router.post("/api/research/drawings/clear")
    async def api_research_drawings_clear(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        return get_workspace().clear_drawings(str(body.get("symbol") or "").upper())

    @router.post("/api/research/drawings/cancel")
    async def api_research_drawings_cancel(user: dict = Depends(require_auth)):
        return get_workspace().cancel_drawing()

    @router.post("/api/research/drawings/style")
    async def api_research_drawings_style(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        return get_workspace().apply_style(
            color=body.get("color"),
            width=body.get("width"),
        )

    @router.get("/api/research/overlays")
    async def api_research_overlays(
        user: dict = Depends(require_auth),
        symbol: str = Query(...),
        timeframe: str = Query("5m"),
        limit: Optional[int] = Query(None, ge=1, le=3000),
    ):
        ws = get_workspace()
        try:
            candles = await asyncio.to_thread(
                candle_objects, symbol, timeframe, limit=limit
            )
        except KeyError:
            return _error(404, "unknown_symbol", f"no 1m candles for {symbol}")
        except ValueError as exc:
            return _error(400, str(exc), str(exc))
        lld_objs, lld_ema, clusters = ws.lld_objects(candles)
        overlays = ws.composed_overlays(str(symbol).upper(), str(timeframe), lld_objs)
        return {
            "success": True,
            "symbol": str(symbol).upper(),
            "timeframe": timeframe,
            "overlays": overlays,
            "lld_ema": lld_ema,
            "clusters": clusters,
        }

    @router.get("/api/research/position")
    async def api_research_position_get(user: dict = Depends(require_auth)):
        pos = get_workspace().selected_position()
        if pos is None:
            return _error(404, "no_position", "no selected position")
        return {"success": True, "position": pos, **get_workspace().snapshot()}

    @router.post("/api/research/position")
    async def api_research_position_post(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        try:
            return get_workspace().update_position(body)
        except KeyError:
            return _error(404, "unknown_drawing", "unknown drawing")
        except ValueError as exc:
            return _error(400, "invalid_position", str(exc))

    return router
