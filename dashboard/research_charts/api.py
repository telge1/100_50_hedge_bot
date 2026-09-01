"""Research Charts HTTP surface. Auth injected from dashboard app."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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
    known_symbols,
    list_symbols,
    load_candles,
    pane_bundle,
    symbol_meta,
)
from .orderbook_profile import (
    MAX_RANGE_SECONDS as OB_PROFILE_MAX_RANGE_SECONDS,
    OrderbookProfileQueryError,
    load_orderbook_profile,
)
from .live_diag import clear_live_diag, record_live_diag, snapshot_live_diag
from .public_trades_profile import (
    VolumeProfileQueryError,
    load_volume_profile,
)
from .trade_bubbles import load_bubbles_payload
from .volume_profile import MAX_RANGE_SECONDS
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

    @router.get("/api/research/volume-profile")
    async def api_research_volume_profile(
        user: dict = Depends(require_auth),
        symbol: str = Query(...),
        start: int = Query(..., description="UTC unix seconds, inclusive"),
        end: int = Query(..., description="UTC unix seconds, exclusive"),
        rows: str = Query("auto"),
        volume_mode: str = Query("base"),
    ):
        """Visible-range volume profile from public_trades_canonical.

        Value area: POC-expand to 70% of total base volume.
        Dedup: ReplacingMergeTree FINAL on (symbol, trade_id) inside the window.
        """
        sym = str(symbol or "").strip().upper()
        try:
            start_dt = datetime.fromtimestamp(int(start), tz=timezone.utc)
            end_dt = datetime.fromtimestamp(int(end), tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return _error(400, "invalid_time_range", "start and end must be UTC unix seconds")
        known = True
        if sym != "XAUUSDT":
            try:
                known = sym in known_symbols()
            except Exception:
                known = True
        try:
            payload = await asyncio.to_thread(
                load_volume_profile,
                symbol=sym,
                start=start_dt,
                end=end_dt,
                rows=rows,
                volume_mode=str(volume_mode or "base"),
                known_symbol=known,
            )
        except KeyError:
            return _error(404, "unknown_symbol", f"unknown symbol {sym}")
        except ValueError as exc:
            code = str(exc)
            if code == "time_range_too_large":
                return _error(
                    400,
                    code,
                    f"time range exceeds {MAX_RANGE_SECONDS} seconds (7 days)",
                )
            return _error(400, code, str(exc))
        except VolumeProfileQueryError as exc:
            return _error(exc.status, exc.code, str(exc))
        except Exception as exc:
            return _error(500, "volume_profile_failed", str(exc))
        return payload

    @router.get("/api/research/trade-bubbles")
    async def api_research_trade_bubbles(
        user: dict = Depends(require_auth),
        symbol: str = Query(...),
        start: int = Query(..., description="UTC unix seconds, inclusive"),
        end: int = Query(..., description="UTC unix seconds, exclusive"),
        as_of: Optional[int] = Query(None, description="Causal cursor UTC unix; default=end"),
        mode: str = Query("large_medium"),
    ):
        """Causal public-trade bubbles for the visible chart window (layer-only).

        Does not start scanner/backtest jobs. Aggregation matches OA public_trade_bubbles.
        """
        sym = str(symbol or "").strip().upper()
        try:
            start_dt = datetime.fromtimestamp(int(start), tz=timezone.utc)
            end_dt = datetime.fromtimestamp(int(end), tz=timezone.utc)
            as_of_dt = (
                datetime.fromtimestamp(int(as_of), tz=timezone.utc)
                if as_of is not None
                else end_dt
            )
        except (TypeError, ValueError, OSError, OverflowError):
            return _error(400, "invalid_time_range", "start/end/as_of must be UTC unix seconds")
        try:
            payload = await asyncio.to_thread(
                load_bubbles_payload,
                symbol=sym,
                start=start_dt,
                end=end_dt,
                as_of=as_of_dt,
                mode=str(mode or "large_medium"),
            )
        except ValueError as exc:
            code = str(exc)
            if code == "time_range_too_large":
                return _error(400, code, "time range exceeds 6 hours")
            return _error(400, code, str(exc))
        except Exception as exc:
            return _error(500, "trade_bubbles_failed", str(exc))
        return payload

    @router.get("/api/research/orderbook-profile")
    async def api_research_orderbook_profile(
        user: dict = Depends(require_auth),
        symbol: str = Query(...),
        start: int = Query(..., description="UTC unix seconds, inclusive"),
        end: int = Query(..., description="UTC unix seconds, exclusive"),
        at: Optional[int] = Query(
            None,
            description="Causal snapshot time (UTC unix). Default: end-ε (current as-of range end).",
        ),
        mode: str = Query(
            "snapshot_at",
            description=(
                "snapshot_at (default: OB200 multi-walls when archive exists, else features) | "
                "ob200 | features | history"
            ),
        ),
    ):
        """Orderbook walls for Research Charts.

        Default ``snapshot_at``: prefer local OB200 multi-level walls when a raw
        archive exists for the symbol; otherwise dominant Bid/Ask from
        ``orderbook_features_1s_v2``. Force with ``ob200`` / ``features``.
        """
        sym = str(symbol or "").strip().upper()
        try:
            start_dt = datetime.fromtimestamp(int(start), tz=timezone.utc)
            end_dt = datetime.fromtimestamp(int(end), tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return _error(400, "invalid_time_range", "start and end must be UTC unix seconds")
        if end_dt <= start_dt:
            return _error(400, "invalid_time_range", "end must be after start")
        at_dt = None
        if at is not None:
            try:
                at_dt = datetime.fromtimestamp(int(at), tz=timezone.utc)
            except (TypeError, ValueError, OSError, OverflowError):
                return _error(400, "invalid_at", "at must be UTC unix seconds")
        mode_s = str(mode or "snapshot_at").strip().lower()
        if mode_s not in {"snapshot_at", "history", "visible_range", "ob200", "features"}:
            return _error(
                400,
                "invalid_mode",
                "mode must be snapshot_at, ob200, features, or history",
            )
        known = True
        if sym != "XAUUSDT":
            try:
                known = sym in known_symbols()
            except Exception:
                known = True
        try:
            payload = await asyncio.to_thread(
                load_orderbook_profile,
                symbol=sym,
                start=start_dt,
                end=end_dt,
                at=at_dt,
                mode=mode_s,
                known_symbol=known,
            )
        except KeyError:
            return _error(404, "unknown_symbol", f"unknown symbol {sym}")
        except ValueError as exc:
            code = str(exc)
            if code == "time_range_too_large":
                return _error(
                    400,
                    code,
                    f"time range exceeds {OB_PROFILE_MAX_RANGE_SECONDS} seconds (7 days)",
                )
            return _error(400, code, str(exc))
        except OrderbookProfileQueryError as exc:
            return _error(exc.status, exc.code, str(exc))
        except Exception as exc:
            return _error(500, "orderbook_profile_failed", str(exc))
        return payload

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
                liquidity_location_as_of=body.get("liquidity_location_as_of"),
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

    @router.post("/api/research/live-diag")
    async def api_research_live_diag_post(
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        """Client-side live-price apply diagnostics (no auth — must work when session dies)."""
        return record_live_diag(body if isinstance(body, dict) else {})

    @router.get("/api/research/live-diag")
    async def api_research_live_diag_get(
        request: Request,
        limit: int = Query(80, ge=1, le=400),
    ):
        # Allow unauthenticated local/ops reads; remote still requires login.
        host = (request.client.host if request.client else "") or ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            require_auth(request)
        return snapshot_live_diag(limit=limit)

    @router.post("/api/research/live-diag/clear")
    async def api_research_live_diag_clear(user: dict = Depends(require_auth)):
        clear_live_diag()
        return {"success": True, "cleared": True}

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
                volume_profile=body.get("volume_profile"),
                orderbook_profile=body.get("orderbook_profile"),
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
        strategy_id = str(body.get("strategy_id") or "").strip()
        if strategy_id in ("cluster_sweep_ema_9_20_59", "cluster_sweep"):
            # Toggle / show last cluster-sweep run markers
            symbol = str(body.get("symbol") or "").strip().upper()
            visible = body.get("visible")
            ws = get_workspace()
            if visible is None:
                cur = bool((ws.snapshot().get("cluster_sweep") or {}).get("visible"))
                visible = not cur
            snap = ws.set_cluster_sweep_visible(bool(visible), symbol or None)
            return snap
        if strategy_id in ("ema_dual_cross_multisource_v1", "ema_dual_cross"):
            symbol = str(body.get("symbol") or "").strip().upper()
            visible = body.get("visible")
            ws = get_workspace()
            if visible is None:
                cur = bool((ws.snapshot().get("ema_dual_cross") or {}).get("visible"))
                visible = not cur
            snap = ws.set_ema_dual_cross_visible(bool(visible), symbol or None)
            return snap
        if strategy_id in ("ema_zone_microstructure_confirmation_v1", "ezm"):
            symbol = str(body.get("symbol") or "").strip().upper()
            visible = body.get("visible")
            layer_mode = body.get("ezm_layer_mode") or body.get("layer_mode")
            layer_only = bool(body.get("layer_only"))
            ws = get_workspace()
            if layer_mode is not None and layer_only:
                snap = ws.set_ezm_layer_mode(str(layer_mode), symbol or None)
                return snap
            if visible is None:
                cur = bool((ws.snapshot().get("ezm") or {}).get("visible"))
                visible = not cur
            snap = ws.set_ezm_visible(
                bool(visible),
                symbol or None,
                layer_mode=str(layer_mode) if layer_mode is not None else None,
            )
            return snap
        if strategy_id in (
            "a_plus_nested_ask_pool_edge_short_v1",
            "nested_ask_pool_edge_short_v1",
            "nested_ask_pool",
        ):
            symbol = str(body.get("symbol") or "").strip().upper()
            visible = body.get("visible")
            ws = get_workspace()
            if visible is None:
                cur = bool((ws.snapshot().get("nested_ask_pool") or {}).get("visible"))
                visible = not cur
            if symbol and body.get("clear_other_strategies"):
                for other in (
                    "stoch_fade",
                    "cluster_sweep_ema_9_20_59",
                    "ema_dual_cross_multisource_v1",
                    "ema_zone_microstructure_confirmation_v1",
                    "a_plus_liquidity_pool_signal_scanner_v1",
                ):
                    ws.clear_backtester_strategy(symbol, strategy_id=other)
            return ws.set_nested_ask_pool_visible(bool(visible), symbol or None)
        if strategy_id in (
            "a_plus_liquidity_pool_signal_scanner_v1",
            "a_plus_pool_signal_scanner_v1",
            "pool_signals",
            "a_plus",
        ):
            from pathlib import Path

            from .pool_signals_backtester import (
                auto_import_latest_for_symbol,
                load_run_dir_payload,
                load_scanner_payload_from_results,
            )

            symbol = str(body.get("symbol") or "").strip().upper()
            display_mode = body.get("display_mode") or body.get("pool_signals_mode")
            layer_only = bool(body.get("layer_only"))
            ws = get_workspace()
            # Never mix A+ pool markers with leftover stoch/wave_fade overlays
            if symbol and (
                body.get("clear_other_strategies")
                or layer_only
                or display_mode is not None
            ):
                ws.clear_backtester_strategy(symbol, strategy_id="stoch_fade")
                ws.clear_backtester_strategy(symbol, strategy_id="cluster_sweep_ema_9_20_59")
                ws.clear_backtester_strategy(symbol, strategy_id="ema_dual_cross_multisource_v1")
                ws.clear_backtester_strategy(symbol, strategy_id="ema_zone_microstructure_confirmation_v1")
                ws.clear_backtester_strategy(symbol, strategy_id="a_plus_nested_ask_pool_edge_short_v1")

            def _ensure_run_for_symbol(sym: str) -> str | None:
                """Import latest results when workspace has no confirmed rows for sym."""
                run = ws._pool_signals_run or {}
                run_sym = str((run.get("meta") or {}).get("symbol") or "").upper()
                has = bool(run.get("confirmed")) and (not sym or not run_sym or run_sym == sym)
                if has and not body.get("force_reimport"):
                    return None
                if body.get("auto_import") is False:
                    return "auto_import_disabled"
                payload = auto_import_latest_for_symbol(sym)
                if payload is None:
                    return f"Keine APS-Results mit confirmed_signals für {sym}"
                ws.store_pool_signals_run(payload)
                return None

            if layer_only and display_mode is not None:
                msg = None
                if symbol and str(display_mode).lower() not in {"off", "aus", "none", "0"}:
                    msg = _ensure_run_for_symbol(symbol)
                snap = ws.set_pool_signals_display_mode(str(display_mode), symbol or None)
                if msg:
                    snap.setdefault("pool_signals", {})["message"] = msg
                    snap.setdefault("backtester", {})["message"] = msg
                elif symbol and (ws._pool_signals_run or {}).get("meta", {}).get("import_path"):
                    ip = (ws._pool_signals_run or {}).get("meta", {}).get("import_path")
                    note = f"import {Path(str(ip)).name}"
                    snap.setdefault("pool_signals", {})["message"] = note
                return snap
            import_path = str(body.get("import_path") or "").strip()
            if import_path:
                p = Path(import_path)
                payload = load_run_dir_payload(p, symbol=symbol or None)
                sym = symbol or str((payload.get("meta") or {}).get("symbol") or "").upper()
                snap = ws.store_pool_signals_run(payload)
                if display_mode is not None:
                    snap = ws.set_pool_signals_display_mode(str(display_mode), sym or None)
                return snap
            if body.get("confirmed") is not None or body.get("result") is not None:
                raw = body.get("result") if body.get("result") is not None else body
                payload = load_scanner_payload_from_results(raw) if "confirmed" in raw else raw
                snap = ws.store_pool_signals_run(payload)
                if display_mode is not None:
                    sym = str((payload.get("meta") or {}).get("symbol") or symbol).upper()
                    snap = ws.set_pool_signals_display_mode(str(display_mode), sym or None)
                elif ws._pool_signals_display_mode != "off":
                    sym = str((payload.get("meta") or {}).get("symbol") or symbol).upper()
                    snap = ws.set_pool_signals_display_mode(ws._pool_signals_display_mode, sym or None)
                return snap
            if display_mode is not None:
                if symbol and str(display_mode).lower() not in {"off", "aus", "none", "0"}:
                    _ensure_run_for_symbol(symbol)
                return ws.set_pool_signals_display_mode(str(display_mode), symbol or None)
            return ws.snapshot()

        from .stoch_backtester import fetch_stoch_signal_rows

        symbol = str(body.get("symbol") or "").strip().upper()
        if not symbol:
            return _error(400, "symbol_required", "symbol required")
        # Clear cluster-sweep markers when loading stoch so strategies don't mix
        get_workspace().clear_backtester_strategy(symbol, strategy_id="cluster_sweep_ema_9_20_59")
        hours = int(body.get("hours") or 48)
        strategy_version = str(body.get("strategy_version") or "").strip() or None
        source = str(body.get("source") or "").strip() or None
        job_id = str(body.get("job_id") or "").strip() or None
        evaluation_id = str(body.get("evaluation_id") or "").strip() or None
        if evaluation_id:
            source = "FROZEN_RESEARCH_EVALUATION"
        out = await asyncio.to_thread(
            fetch_stoch_signal_rows,
            symbol=symbol,
            hours=hours,
            strategy_version=strategy_version,
            source=source,
            job_id=job_id,
            evaluation_id=evaluation_id,
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
        if source == "FROZEN_RESEARCH_EVALUATION" or evaluation_id:
            first = rows[0] if rows else {}
            job = job_meta if isinstance(job_meta, dict) else {}
            bt["source"] = "FROZEN_RESEARCH_EVALUATION"
            bt["job_id"] = job_id or job.get("job_id")
            bt["evaluation_id"] = evaluation_id
            bt["strategy_version"] = "wave_fade_frozen_f16ae32"
            bt["exit_policy"] = "NO_BE50"
            bt["outcomes_computed"] = True
            bt["display_mode"] = "FROZEN_NO_BE50_EVALUATED"
            bt["signal_strategy_version"] = "wave_fade_frozen_f16ae32"
            bt["signal_start"] = job.get("signal_start") or first.get("job_signal_start")
            bt["signal_end_exclusive"] = job.get("signal_end_exclusive") or first.get(
                "job_signal_end_exclusive"
            )
            bt["message"] = "NO_BE50 · SL_FIRST · Entry/TP/SL/Exit · WIN/LOSS/OPEN · PnL basis gross"
        elif source == "FROZEN_RESEARCH_JOB" or job_id:
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
        bt["strategy_id"] = "stoch_fade"
        snap["backtester"] = bt
        return snap

    @router.post("/api/research/backtester/run")
    async def api_research_backtester_run(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        """Run a research strategy backtest. Stoch Fade remains job-based."""
        strategy_id = str(body.get("strategy_id") or "").strip()
        symbol = str(body.get("symbol") or "").strip().upper()
        if not symbol:
            return _error(400, "symbol_required", "symbol required")
        if "," in symbol or " " in symbol:
            return _error(400, "single_symbol_required", "exactly one symbol per run")
        start_raw = body.get("start")
        end_raw = body.get("end")
        if not start_raw or not end_raw:
            return _error(400, "range_required", "start and end required (ISO UTC)")
        try:
            start = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
        except ValueError:
            return _error(400, "invalid_time", "start/end must be ISO UTC")

        if strategy_id in ("ema_dual_cross_multisource_v1", "ema_dual_cross"):
            from .ema_dual_cross_backtester import run_ema_dual_cross_backtest

            timeframe = str(body.get("timeframe") or "15m").strip() or "15m"
            try:
                edc_kw: dict[str, Any] = {}
                if "enable_sync_cross" in body:
                    edc_kw["enable_sync_cross"] = bool(body.get("enable_sync_cross"))
                if "enable_compressed_rebound" in body:
                    edc_kw["enable_compressed_rebound"] = bool(body.get("enable_compressed_rebound"))
                result = await asyncio.to_thread(
                    run_ema_dual_cross_backtest,
                    symbol=symbol,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                    show_candidates=bool(body.get("show_candidates", True)),
                    show_allow=bool(body.get("show_allow", True)),
                    show_block=bool(body.get("show_block", False)),
                    show_inconclusive=bool(body.get("show_inconclusive", False)),
                    show_rejected=bool(body.get("show_rejected", False)),
                    **edc_kw,
                )
            except KeyError:
                return _error(404, "unknown_symbol", f"unknown symbol {symbol}")
            except ValueError as exc:
                return _error(400, "invalid_params", str(exc))
            except Exception as exc:  # noqa: BLE001
                return _error(503, "ema_dual_cross_failed", str(exc))
            ws = get_workspace()
            ws.clear_backtester_strategy(symbol, strategy_id="cluster_sweep_ema_9_20_59")
            ws.clear_backtester_strategy(symbol, strategy_id="stoch_fade")
            ws.clear_backtester_strategy(symbol, strategy_id="ema_zone_microstructure_confirmation_v1")
            snap = ws.store_ema_dual_cross_run(result)
            snap["ema_dual_cross_result"] = {
                "meta": result.get("meta"),
                "coverage": result.get("coverage"),
                "summary": result.get("summary"),
                "n_candidates": len(result.get("candidates") or []),
                "candidates": result.get("candidates"),
            }
            return snap

        if strategy_id in (
            "a_plus_liquidity_pool_signal_scanner_v1",
            "a_plus_pool_signal_scanner_v1",
            "pool_signals",
            "a_plus",
        ):
            from .pool_signals_backtester import run_pool_signals_backtest

            try:
                payload = await asyncio.to_thread(
                    run_pool_signals_backtest,
                    symbol=symbol,
                    start=start,
                    end=end,
                )
            except ValueError as exc:
                return _error(400, "invalid_params", str(exc))
            except Exception as exc:  # noqa: BLE001
                return _error(503, "pool_signals_failed", str(exc))
            ws = get_workspace()
            ws.clear_backtester_strategy(symbol, strategy_id="stoch_fade")
            ws.clear_backtester_strategy(symbol, strategy_id="cluster_sweep_ema_9_20_59")
            ws.clear_backtester_strategy(symbol, strategy_id="ema_dual_cross_multisource_v1")
            ws.clear_backtester_strategy(symbol, strategy_id="ema_zone_microstructure_confirmation_v1")
            ws.clear_backtester_strategy(symbol, strategy_id="a_plus_nested_ask_pool_edge_short_v1")
            snap = ws.store_pool_signals_run(payload)
            mode = str(body.get("display_mode") or "confirmed")
            snap = ws.set_pool_signals_display_mode(mode, symbol)
            snap["pool_signals_result"] = {
                "meta": payload.get("meta"),
                "n_confirmed": len(payload.get("confirmed") or []),
                "n_debug_rows": len(payload.get("debug_rows") or []),
            }
            return snap

        if strategy_id in (
            "a_plus_nested_ask_pool_edge_short_v1",
            "nested_ask_pool_edge_short_v1",
            "nested_ask_pool",
        ):
            from .nested_ask_pool_jobs import start_nested_ask_pool_job

            start = body.get("start") or body.get("signal_start")
            end = body.get("end") or body.get("signal_end_exclusive") or body.get("end_exclusive")
            payload, code = await asyncio.to_thread(
                start_nested_ask_pool_job,
                symbol=symbol,
                start=str(start or ""),
                end=str(end or ""),
                show_rejected=bool(body.get("show_rejected")),
            )
            if code != 200:
                return JSONResponse(payload, status_code=code)
            return payload

        if strategy_id not in ("cluster_sweep_ema_9_20_59", "cluster_sweep"):
            return _error(
                400,
                "unsupported_strategy",
                "Unterstützt: cluster_sweep_ema_9_20_59, ema_dual_cross_multisource_v1, "
                "a_plus_liquidity_pool_signal_scanner_v1, a_plus_nested_ask_pool_edge_short_v1",
            )
        from .cluster_sweep_backtester import run_cluster_sweep_backtest

        timeframe = str(body.get("timeframe") or "5m").strip() or "5m"
        debug_low = bool(body.get("debug_low_pool") or body.get("debug_low_pool_zones"))
        min_pools = int(body.get("minimum_cluster_pools") or (1 if debug_low else 3))
        try:
            result = await asyncio.to_thread(
                run_cluster_sweep_backtest,
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                minimum_cluster_pools=min_pools,
                ema_fast=int(body.get("ema_fast") or 9),
                ema_medium=int(body.get("ema_medium") or 20),
                ema_slow=int(body.get("ema_slow") or 59),
                show_detail_markers=bool(body.get("show_detail_markers")),
                debug_low_pool=debug_low,
                expire_bars=int(body.get("expire_bars") or 24),
            )
        except KeyError:
            return _error(404, "unknown_symbol", f"unknown symbol {symbol}")
        except ValueError as exc:
            return _error(400, "invalid_params", str(exc))
        except Exception as exc:  # noqa: BLE001
            return _error(503, "cluster_sweep_failed", str(exc))

        # Clear other strategy markers for this symbol
        get_workspace().clear_backtester_strategy(symbol, strategy_id="stoch_fade")
        get_workspace().clear_backtester_strategy(symbol, strategy_id="ema_dual_cross_multisource_v1")
        get_workspace().clear_backtester_strategy(symbol, strategy_id="ema_zone_microstructure_confirmation_v1")
        snap = get_workspace().store_cluster_sweep_run(result)
        snap["cluster_sweep_result"] = {
            "meta": result.get("meta"),
            "coverage": result.get("coverage"),
            "n_events": len(result.get("events") or []),
            "events": result.get("events"),
        }
        return snap

    @router.post("/api/research/backtester/cluster-sweep/nav")
    async def api_research_cluster_sweep_nav(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        delta = body.get("delta")
        index = body.get("index")
        return get_workspace().navigate_cluster_sweep_event(
            delta=int(delta or 0),
            index=None if index is None else int(index),
        )

    @router.post("/api/research/backtester/ema-dual-cross/nav")
    async def api_research_ema_dual_cross_nav(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        delta = body.get("delta")
        index = body.get("index")
        return get_workspace().navigate_ema_dual_cross_candidate(
            delta=int(delta or 0),
            index=None if index is None else int(index),
        )

    @router.get("/api/research/nested-ask-pool/status")
    async def api_research_nested_ask_pool_status(
        user: dict = Depends(require_auth),
        job_id: str = Query(...),
    ):
        from .nested_ask_pool_jobs import nested_ask_pool_job_status

        payload, code = await asyncio.to_thread(nested_ask_pool_job_status, job_id)
        if code != 200:
            return JSONResponse(payload, status_code=code)
        return payload

    @router.post("/api/research/nested-ask-pool/import")
    async def api_research_nested_ask_pool_import(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        from .nested_ask_pool_jobs import import_nested_ask_pool_job_to_workspace

        payload, code = await asyncio.to_thread(
            import_nested_ask_pool_job_to_workspace,
            job_id=str(body.get("job_id") or ""),
        )
        if code != 200:
            return JSONResponse(payload, status_code=code)
        return payload

    @router.post("/api/research/ezm/run")
    async def api_research_ezm_run(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        from .ezm_jobs import start_ezm_research_job

        payload, code = await asyncio.to_thread(
            start_ezm_research_job,
            symbol=str(body.get("symbol") or ""),
            start=str(body.get("start") or body.get("signal_start") or ""),
            end=str(body.get("end") or body.get("signal_end_exclusive") or ""),
            computation_mode=str(body.get("computation_mode") or "") or None,
        )
        if code != 200:
            return JSONResponse(payload, status_code=code)
        return payload

    @router.get("/api/research/ezm/status")
    async def api_research_ezm_status(
        user: dict = Depends(require_auth),
        job_id: str = Query(...),
    ):
        from .ezm_jobs import ezm_job_status

        payload, code = await asyncio.to_thread(ezm_job_status, job_id)
        if code != 200:
            return JSONResponse(payload, status_code=code)
        return payload

    @router.post("/api/research/ezm/import")
    async def api_research_ezm_import(
        user: dict = Depends(require_auth),
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        from .ezm_jobs import import_ezm_job_to_workspace

        payload, code = await asyncio.to_thread(
            import_ezm_job_to_workspace,
            job_id=str(body.get("job_id") or ""),
            symbol=str(body.get("symbol") or ""),
        )
        if code != 200:
            return JSONResponse(payload, status_code=code)
        return payload

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
