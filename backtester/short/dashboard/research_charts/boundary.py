"""Research engine boundary — Phase 1 contracts, no live data plane.

Target architecture:

    ClickHouse / Realtime Data
            ↓
    Research Python Engine  (trading_research_platform: data/, indicators/, overlays/)
            ↓
    Research API            (this package)
            ↓
    Dashboard Research Charts

Forbidden:
- Frontend reads ClickHouse
- JavaScript computes Liquidity / Stochastic / TF aggregation
- Importing PySide6 / QWebEngine / charts.bridge.ChartBridge into the web runtime
"""

from __future__ import annotations

from pathlib import Path

PHASE_1_FEED_READY = False

TRP_ROOT = Path("/home/telgenbuescher/projects/trading_research_platform")

# Recommended Phase-2 wiring: adapter imports TRP packages via sys.path insert of
# TRP_ROOT. Do NOT pip-copy engines. Do NOT add TRP's `app` package to sys.path
# (name collision with dashboard/app.py).
PHASE_2_IMPORT_PLAN = {
    "candles": "data.models.Candle, data.timeframes.aggregate / floor_utc / SUPPORTED_TIMEFRAMES",
    "source_abc": "data.models.DataSource — implement ClickHouse/MySQL adapter, do not use LocalCsvSource in prod",
    "ema": "indicators.ema_overlays.EmaOverlayEngine, ema_overlays_payload",
    "stochastic": "indicators.stochastic.StochasticEngine, stochastic_payload",
    "liquidity": "indicators.liquidity_location.engine.LiquidityLocationEngine + compose_lld_overlays",
    "overlays": "overlays.serialization.serialize_overlays",
}

REUSE_DIRECTLY = [
    "data.models",
    "data.timeframes",
    "data.cache",
    "data.validation",
    "indicators.ema",
    "indicators.ema_overlays",
    "indicators.stochastic",
    "indicators.liquidity_location",
    "overlays.models",
    "overlays.manager",
    "overlays.serialization",
    "indicators.settings_store",
    "indicators.registry",
]

ADAPT_FOR_WEB = [
    "charts/web/chart.js (strip QWebChannel; v4.2.3 vs dashboard v5.0.7)",
    "charts/web/style.css tokens",
    "charts/bridge.py candles_to_js / build_chart_payload (functions only, not ChartBridge)",
    "app.workspace LAYOUT_1/2H/2V/4 semantics (reimplemented in research/workspace.py)",
]

DESKTOP_ONLY = [
    "app.main",
    "app.main_window",
    "app.chart_window",
    "app.chart_pane",
    "charts.bridge.ChartBridge",
    "PySide6",
    "QWebEngineView",
    "QDialog settings windows",
]

FORBIDDEN_WEB_IMPORTS = (
    "PySide6",
    "PyQt6",
    "PyQt5",
    "app.main",
    "app.main_window",
    "app.chart_window",
    "charts.bridge",
)

SUPPORTED_LAYOUTS = ("1", "2H", "2V", "4")
SUPPORTED_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h")
PANE_COUNT = {"1": 1, "2H": 2, "2V": 2, "4": 4}

INDICATOR_CONTRACTS = (
    {
        "id": "liquidity_location",
        "label": "Liquidity Location",
        "engine": "indicators.liquidity_location.engine.LiquidityLocationEngine",
        "payload": "compose_lld_overlays + serialize_overlays + lld_ema_payload",
        "compute_in": "python",
    },
    {
        "id": "ema_overlays",
        "label": "EMA",
        "engine": "indicators.ema_overlays.EmaOverlayEngine",
        "payload": "ema_overlays_payload",
        "compute_in": "python",
    },
    {
        "id": "stochastic",
        "label": "Stochastic",
        "engine": "indicators.stochastic.StochasticEngine",
        "payload": "stochastic_payload → setLowerPane",
        "compute_in": "python",
    },
)

STREAM_CONTRACT = {
    "ready": False,
    "proposed_path": "/api/research/stream",
    "transports": ["sse", "websocket"],
    "preferred": "sse",
    "reuse_existing": [
        "GET /api/live-orderbook/stream (SSE pattern)",
        "WS /ws/positions/{account}/{symbol} (WS pattern)",
    ],
    "payload_sketch": {
        "type": "candle_update",
        "symbol": "APTUSDT",
        "timeframe": "1m",
        "candle": {"time": 0, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0},
        "is_closed": False,
    },
    "note": "Phase 3 uses 5s ClickHouse incremental polling. SSE optional later.",
}

FEED_MESSAGE = (
    "Research Charts Phase 1: UI + API contracts only. "
    "Demo candles are placeholders. ClickHouse / Realtime / TRP engines "
    "are wired in Phase 2."
)
