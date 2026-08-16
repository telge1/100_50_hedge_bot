"""Collector health state and metrics (NO SILENT DATA LOSS runtime view)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class CollectorState(str, Enum):
    STARTING = "STARTING"
    RECOVERING = "RECOVERING"
    CONNECTING = "CONNECTING"
    SUBSCRIBING = "SUBSCRIBING"
    LIVE = "LIVE"
    RECONNECTING = "RECONNECTING"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class SymbolRuntimeState(str, Enum):
    STARTING = "STARTING"
    RECOVERING = "RECOVERING"
    SUBSCRIBING = "SUBSCRIBING"
    LIVE = "LIVE"
    STALE = "STALE"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


@dataclass
class SymbolHealth:
    symbol: str
    configured: bool = True
    subscribed: bool = False
    state: SymbolRuntimeState = SymbolRuntimeState.STARTING
    last_websocket_update_at: datetime | None = None
    last_closed_candle_at: datetime | None = None
    last_persisted_open_time: datetime | None = None
    candle_lag_seconds: float | None = None
    recovery_state: str = "IDLE"
    signal_processor_state: str = "IDLE"
    signal_processing_lag_seconds: float | None = None
    signal_last_processed_at: datetime | None = None
    signal_last_error: str | None = None
    last_error: str | None = None
    recovery_candles_inserted: int = 0
    subscribe_acked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "configured": self.configured,
            "subscribed": self.subscribed,
            "state": self.state.value,
            "last_websocket_update_at": _iso(self.last_websocket_update_at),
            "last_closed_candle_at": _iso(self.last_closed_candle_at),
            "last_persisted_open_time": _iso(self.last_persisted_open_time),
            "candle_lag_seconds": self.candle_lag_seconds,
            "recovery_state": self.recovery_state,
            "signal_processor_state": self.signal_processor_state,
            "signal_processing_lag_seconds": self.signal_processing_lag_seconds,
            "signal_last_processed_at": _iso(self.signal_last_processed_at),
            "signal_last_error": self.signal_last_error,
            "last_error": self.last_error,
            "recovery_candles_inserted": self.recovery_candles_inserted,
            "subscribe_acked": self.subscribe_acked,
        }


@dataclass
class HealthState:
    state: CollectorState = CollectorState.STARTING
    desired_state: str = "STOPPED"
    websocket_connected: bool = False
    connected_at: datetime | None = None
    started_at: datetime | None = None
    last_message_at: datetime | None = None
    last_ping_at: datetime | None = None
    last_pong_at: datetime | None = None
    reconnect_count: int = 0
    consecutive_reconnect_failures: int = 0
    recovery_run_count: int = 0
    recovery_gap_count: int = 0
    recovery_candles_inserted: int = 0
    last_recovery_at: datetime | None = None
    last_closed_candle_by_symbol: dict[str, datetime] = field(default_factory=dict)
    forming_by_symbol: dict[str, dict[str, Any]] = field(default_factory=dict)
    symbols_total: int = 0
    symbols_live: int = 0
    symbols_recovering: int = 0
    symbols_stale: set[str] = field(default_factory=set)
    last_error: str | None = None
    shadow_mode: bool = True
    trading_enabled: bool = False
    symbol_health: dict[str, SymbolHealth] = field(default_factory=dict)
    configured_symbols: list[str] = field(default_factory=list)
    subscribed_symbols: set[str] = field(default_factory=set)
    invalid_symbols: list[dict[str, str]] = field(default_factory=list)
    candle_symbols: list[str] = field(default_factory=list)
    signal_symbols: list[str] = field(default_factory=list)
    # Live signal worker pool metrics (optional; filled by SignalWorkerPool)
    signal_queue_depth: int = 0
    signal_queue_maxsize: int = 0
    signal_oldest_job_age_seconds: float | None = None
    signal_workers_total: int = 0
    signal_workers_busy: int = 0
    signal_jobs_processed: int = 0
    signal_jobs_failed: int = 0
    signal_jobs_retried: int = 0
    signal_processing_last_ms: float | None = None
    signal_processing_max_ms: float = 0.0
    max_signal_lag_seconds: float | None = None
    signal_dirty_symbols: list[str] = field(default_factory=list)
    signal_overflow_count: int = 0
    _signal_metrics_provider: Callable[[], dict[str, Any]] | None = field(
        default=None, repr=False, compare=False
    )

    def set_forming_candle(self, symbol: str, payload: dict[str, Any] | None) -> None:
        sym = str(symbol or "").upper()
        if not sym:
            return
        if payload is None:
            self.forming_by_symbol.pop(sym, None)
            return
        self.forming_by_symbol[sym] = dict(payload)

    def forming_payload(self, symbol: str | None = None) -> dict[str, Any]:
        if symbol:
            sym = str(symbol).upper()
            return {
                "symbol": sym,
                "forming": self.forming_by_symbol.get(sym),
            }
        return {"forming_by_symbol": dict(self.forming_by_symbol)}

    def ensure_symbol(self, symbol: str) -> SymbolHealth:
        sym = symbol.upper()
        if sym not in self.symbol_health:
            self.symbol_health[sym] = SymbolHealth(symbol=sym)
        return self.symbol_health[sym]

    def init_symbols(self, symbols: list[str]) -> None:
        self.configured_symbols = [s.upper() for s in symbols]
        self.symbols_total = len(self.configured_symbols)
        if not self.candle_symbols:
            self.candle_symbols = list(self.configured_symbols)
        for s in self.configured_symbols:
            self.ensure_symbol(s)

    def set_state(self, new_state: CollectorState, *, reason: str = "") -> None:
        if self.state == new_state:
            return
        old = self.state
        self.state = new_state
        msg = f"STATE {old.value} → {new_state.value}"
        if reason:
            msg += f" ({reason})"
        logger.info(msg)

    def note_message(self) -> None:
        self.last_message_at = _utcnow()

    def note_ping(self) -> None:
        now = _utcnow()
        self.last_ping_at = now
        self.last_message_at = now

    def note_pong(self) -> None:
        now = _utcnow()
        self.last_pong_at = now
        self.last_message_at = now

    def note_connected(self) -> None:
        self.websocket_connected = True
        self.connected_at = _utcnow()
        self.consecutive_reconnect_failures = 0

    def note_disconnected(self) -> None:
        self.websocket_connected = False
        self.subscribed_symbols.clear()
        self.forming_by_symbol.clear()
        for sh in self.symbol_health.values():
            sh.subscribed = False
            sh.subscribe_acked = False
            if sh.state not in (SymbolRuntimeState.ERROR, SymbolRuntimeState.STOPPED):
                if self.state in (CollectorState.STOPPING, CollectorState.STOPPED):
                    sh.state = SymbolRuntimeState.STOPPED
                else:
                    sh.state = SymbolRuntimeState.STARTING

    def mark_subscribed(self, symbol: str, *, acked: bool = True) -> None:
        sym = symbol.upper()
        self.subscribed_symbols.add(sym)
        sh = self.ensure_symbol(sym)
        sh.subscribed = True
        sh.subscribe_acked = acked
        if sh.state in (
            SymbolRuntimeState.STARTING,
            SymbolRuntimeState.RECOVERING,
            SymbolRuntimeState.SUBSCRIBING,
        ):
            sh.state = SymbolRuntimeState.SUBSCRIBING

    def mark_symbol_live(self, symbol: str) -> None:
        sh = self.ensure_symbol(symbol)
        if sh.state != SymbolRuntimeState.ERROR:
            sh.state = SymbolRuntimeState.LIVE
            sh.recovery_state = "IDLE"

    def refresh_aggregates(self) -> None:
        live = [
            s
            for s, sh in self.symbol_health.items()
            if sh.state == SymbolRuntimeState.LIVE and s in self.configured_symbols
        ]
        recovering = [
            s
            for s, sh in self.symbol_health.items()
            if sh.state == SymbolRuntimeState.RECOVERING
        ]
        self.symbols_live = len(live)
        self.symbols_recovering = len(recovering)
        self.symbols_stale = {
            s
            for s, sh in self.symbol_health.items()
            if sh.state == SymbolRuntimeState.STALE
        }

    def update_candle_lag(self, *, as_of: datetime | None = None) -> None:
        now = as_of or _utcnow()
        for sh in self.symbol_health.values():
            if sh.last_persisted_open_time is None:
                sh.candle_lag_seconds = None
                continue
            # Expected close of last persisted bar + lag to now
            expected_close = sh.last_persisted_open_time
            from datetime import timedelta

            age = (now - expected_close - timedelta(minutes=1)).total_seconds()
            sh.candle_lag_seconds = max(0.0, age)

    def to_dict(self) -> dict[str, Any]:
        if self._signal_metrics_provider is not None:
            try:
                self.apply_signal_pool_metrics(self._signal_metrics_provider())
            except Exception:  # noqa: BLE001
                logger.exception("signal metrics provider failed")
        self.refresh_aggregates()
        self.update_candle_lag()
        live_symbols = sorted(
            s
            for s, sh in self.symbol_health.items()
            if sh.state == SymbolRuntimeState.LIVE
        )
        recovering_symbols = sorted(
            s
            for s, sh in self.symbol_health.items()
            if sh.state == SymbolRuntimeState.RECOVERING
        )
        stale_symbols = sorted(self.symbols_stale)
        return {
            "desired_state": self.desired_state,
            "collector_state": self.state.value,
            # backward compatible alias
            "state": self.state.value,
            "websocket_connected": self.websocket_connected,
            "configured_symbols": list(self.configured_symbols),
            "candle_symbols": list(self.candle_symbols or self.configured_symbols),
            "signal_symbols": list(self.signal_symbols),
            "subscribed_symbols": sorted(self.subscribed_symbols),
            "live_symbols": live_symbols,
            "stale_symbols": stale_symbols,
            "stale_candle_symbols": stale_symbols,
            "recovering_symbols": recovering_symbols,
            "recovery_status": {
                "run_count": self.recovery_run_count,
                "gap_count": self.recovery_gap_count,
                "candles_inserted": self.recovery_candles_inserted,
                "last_recovery_at": _iso(self.last_recovery_at),
                "per_symbol": {
                    s: self.symbol_health[s].recovery_state
                    for s in (self.candle_symbols or self.configured_symbols)
                    if s in self.symbol_health
                },
            },
            "last_persisted_by_candle_symbol": {
                k: v.isoformat()
                for k, v in sorted(self.last_closed_candle_by_symbol.items())
            },
            "configured_count": len(self.configured_symbols),
            "subscribed_count": len(self.subscribed_symbols),
            "live_count": len(live_symbols),
            "last_message_at": _iso(self.last_message_at),
            "last_ping_at": _iso(self.last_ping_at),
            "last_pong_at": _iso(self.last_pong_at),
            "reconnect_count": self.reconnect_count,
            "started_at": _iso(self.started_at),
            "connected_at": _iso(self.connected_at),
            "consecutive_reconnect_failures": self.consecutive_reconnect_failures,
            "recovery_run_count": self.recovery_run_count,
            "recovery_gap_count": self.recovery_gap_count,
            "recovery_candles_inserted": self.recovery_candles_inserted,
            "last_recovery_at": _iso(self.last_recovery_at),
            "last_closed_candle_by_symbol": {
                k: v.isoformat()
                for k, v in sorted(self.last_closed_candle_by_symbol.items())
            },
            "symbols_total": self.symbols_total,
            "symbols_live": self.symbols_live,
            "symbols_recovering": self.symbols_recovering,
            "last_error": self.last_error,
            "shadow_mode": self.shadow_mode,
            "trading_enabled": self.trading_enabled,
            "invalid_symbols": list(self.invalid_symbols),
            "signal_queue_depth": self.signal_queue_depth,
            "signal_queue_maxsize": self.signal_queue_maxsize,
            "signal_oldest_job_age_seconds": self.signal_oldest_job_age_seconds,
            "signal_workers_total": self.signal_workers_total,
            "signal_workers_busy": self.signal_workers_busy,
            "signal_jobs_processed": self.signal_jobs_processed,
            "signal_jobs_failed": self.signal_jobs_failed,
            "signal_jobs_retried": self.signal_jobs_retried,
            "signal_processing_last_ms": self.signal_processing_last_ms,
            "signal_processing_max_ms": self.signal_processing_max_ms,
            "max_signal_lag_seconds": self.max_signal_lag_seconds,
            "signal_dirty_symbols": list(self.signal_dirty_symbols),
            "signal_overflow_count": self.signal_overflow_count,
            "symbols": [
                self.symbol_health[s].to_dict()
                for s in self.configured_symbols
                if s in self.symbol_health
            ],
        }

    def apply_signal_pool_metrics(self, metrics: dict[str, Any]) -> None:
        self.signal_queue_depth = int(metrics.get("signal_queue_depth") or 0)
        self.signal_queue_maxsize = int(metrics.get("signal_queue_maxsize") or 0)
        self.signal_oldest_job_age_seconds = metrics.get(
            "signal_oldest_job_age_seconds"
        )
        self.signal_workers_total = int(metrics.get("signal_workers_total") or 0)
        self.signal_workers_busy = int(metrics.get("signal_workers_busy") or 0)
        self.signal_jobs_processed = int(metrics.get("signal_jobs_processed") or 0)
        self.signal_jobs_failed = int(metrics.get("signal_jobs_failed") or 0)
        self.signal_jobs_retried = int(metrics.get("signal_jobs_retried") or 0)
        self.signal_processing_last_ms = metrics.get("signal_processing_last_ms")
        self.signal_processing_max_ms = float(
            metrics.get("signal_processing_max_ms") or 0.0
        )
        if metrics.get("max_signal_lag_seconds") is not None:
            self.max_signal_lag_seconds = metrics.get("max_signal_lag_seconds")
        dirty = metrics.get("signal_dirty_symbols") or []
        self.signal_dirty_symbols = list(dirty)
        self.signal_overflow_count = int(metrics.get("signal_overflow_count") or 0)
