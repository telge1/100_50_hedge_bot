"""Bybit live 1m collector: recovery-first WebSocket ingest + shadow signals.

NO SILENT CANDLE LOSS / NO SILENT SIGNAL LOSS. Shadow mode only — no trading.

LIVE path: WS recv → persist candle → enqueue signal job → return immediately.
Startup/reconnect: blocking candle repair + blocking signal catch-up before LIVE.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Sequence

from signal_generator.bybit.history import BybitHistoryClient, ensure_utc
from signal_generator.bybit.live.candle_universe import (
    assert_no_signal_btc,
    filter_signal_demand,
    normalize_symbols,
    resolve_universes,
)
from signal_generator.bybit.live.candle_buffer import CandleInsertBuffer
from signal_generator.bybit.live.health import (
    CollectorState,
    HealthState,
    SymbolRuntimeState,
)
from signal_generator.bybit.live.recovery import (
    last_fully_closed_open_time,
    recover_symbols,
    recover_symbols_full,
    repair_recent_continuity,
)
from signal_generator.bybit.live.signal_catchup import (
    assert_shadow_only,
    htf_boundary_at_close,
    run_signal_catchup,
    signal_catchup_end_exclusive,
)
from signal_generator.bybit.live.signal_queue import (
    DEFAULT_SIGNAL_QUEUE_MAXSIZE,
    DEFAULT_SIGNAL_WORKERS,
    DEFAULT_SHUTDOWN_DRAIN_S,
    REASON_LIVE_HTF_BOUNDARY,
    REASON_STALE_RECOVERY,
    SignalWorkerPool,
    build_live_catchup_callable,
)
from signal_generator.bybit.live.outcome_queue import (
    OutcomeWorkerPool,
    build_live_outcome_callable,
)
from signal_generator.bybit.live.ws_kline import BybitKlineWebSocket
from signal_generator.config import get_clickhouse_settings
from signal_generator.db.candles import Candle1m, CandleRepository
from signal_generator.db.client import ClickHouseClient
from signal_generator.db.processing_state import ProcessingStateRepository
from signal_generator.db.signals import SignalRepository
from signal_generator.db.public_trades import CanonicalPublicTradeRepository

logger = logging.getLogger(__name__)

BACKOFF_SCHEDULE_S = (1, 2, 5, 10, 30)
DEFAULT_STALE_SYMBOL_MINUTES = 3
DEFAULT_RECENT_CONTINUITY_MINUTES = 120


class Live1mCollector:
    """Live 1m collector with missing-range recovery + shadow signal catch-up."""

    def __init__(
        self,
        *,
        symbols: Sequence[str] | None = None,
        ch: ClickHouseClient,
        history: BybitHistoryClient | None = None,
        stale_symbol_minutes: float = DEFAULT_STALE_SYMBOL_MINUTES,
        ws_factory: type[BybitKlineWebSocket] = BybitKlineWebSocket,
        enable_signals: bool = True,
        repair_internal: bool = True,
        internal_lookback_days: int = 30,
        recent_continuity_minutes: int = DEFAULT_RECENT_CONTINUITY_MINUTES,
        batch_max_rows: int = 50,
        batch_flush_interval_s: float = 0.5,
        desired_state: str = "RUNNING",
        signal_workers: int = DEFAULT_SIGNAL_WORKERS,
        signal_queue_maxsize: int = DEFAULT_SIGNAL_QUEUE_MAXSIZE,
        signal_shutdown_drain_s: float = DEFAULT_SHUTDOWN_DRAIN_S,
        candle_symbols: Sequence[str] | None = None,
        signal_symbols: Sequence[str] | None = None,
        enable_public_trades: bool = False,
        public_trade_symbols: Sequence[str] | None = None,
        public_trade_queue_maxsize: int = 5000,
        public_trade_batch_size: int = 500,
    ) -> None:
        assert_shadow_only()
        if candle_symbols is None:
            if symbols is None:
                raise ValueError("symbols or candle_symbols required")
            # Legacy combined path: live_universe / demand — BTC still forbidden.
            combined = normalize_symbols(symbols)
            if "BTCUSDT" in combined:
                raise ValueError("BTCUSDT must not be subscribed by the live collector")
            self.candle_symbols = combined
            self.signal_symbols = filter_signal_demand(
                signal_symbols if signal_symbols is not None else combined
            )
        else:
            candles, signals = resolve_universes(
                candle_symbols=candle_symbols,
                signal_symbols=(
                    signal_symbols if signal_symbols is not None else []
                ),
            )
            self.candle_symbols = candles
            self.signal_symbols = signals
        assert_no_signal_btc(self.signal_symbols)
        self._signal_symbol_set = set(self.signal_symbols)
        # Candle ingest set (WS + REST). Kept as `symbols` for existing call sites.
        self.symbols = list(self.candle_symbols)
        self.ch = ch
        self._ch_settings = get_clickhouse_settings()
        self.repo = CandleRepository(ch)
        self.signals_repo = SignalRepository(ch)
        self.state_repo = ProcessingStateRepository(ch)
        self.history = history or BybitHistoryClient(request_pause_s=0.05)
        self.stale_symbol_minutes = stale_symbol_minutes
        self.ws_factory = ws_factory
        self.enable_signals = enable_signals
        self.repair_internal = repair_internal
        self.internal_lookback_days = internal_lookback_days
        self.recent_continuity_minutes = recent_continuity_minutes
        self.buffer = CandleInsertBuffer(
            self.repo,
            max_rows=batch_max_rows,
            flush_interval_s=batch_flush_interval_s,
        )
        self.health = HealthState(
            symbols_total=len(self.candle_symbols),
            desired_state=desired_state,
            shadow_mode=True,
            trading_enabled=False,
        )
        self.health.init_symbols(self.candle_symbols)
        self.health.candle_symbols = list(self.candle_symbols)
        self.health.signal_symbols = list(self.signal_symbols)
        self.health.signal_queue_maxsize = signal_queue_maxsize
        self.health.signal_workers_total = (
            signal_workers if enable_signals and self.signal_symbols else 0
        )
        self._stop = asyncio.Event()
        self._ws: BybitKlineWebSocket | None = None
        self._insert_lock = asyncio.Lock()
        self._signal_lock = asyncio.Lock()  # startup/blocking catch-up only
        self._instance_lock_path: Path | None = None
        self._signal_pool: SignalWorkerPool | None = None
        self._outcome_pool: OutcomeWorkerPool | None = None
        self._signal_workers = signal_workers
        self._signal_queue_maxsize = signal_queue_maxsize
        self._signal_shutdown_drain_s = signal_shutdown_drain_s
        self.enable_public_trades = bool(enable_public_trades)
        candle_set = set(self.candle_symbols)
        if public_trade_symbols is None:
            pt_syms = list(self.candle_symbols) if self.enable_public_trades else []
        else:
            pt_syms = [s.upper() for s in public_trade_symbols]
        if self.enable_public_trades:
            extra = [s for s in pt_syms if s not in candle_set]
            if extra:
                raise ValueError(
                    "public_trade_symbols must be a subset of candle universe: "
                    + ",".join(extra)
                )
            if "XAUUSDT" in pt_syms:
                raise ValueError("XAUUSDT must not be subscribed for public trades")
        self.public_trade_symbols = pt_syms if self.enable_public_trades else []
        self._public_trade_queue_maxsize = public_trade_queue_maxsize
        self._public_trade_batch_size = public_trade_batch_size
        self._trade_buffer = None
        self.health.public_trades_enabled = self.enable_public_trades
        self.health.public_trade_symbols = list(self.public_trade_symbols)

    def set_desired_state(self, value: str) -> None:
        self.health.desired_state = value

    def request_stop(self) -> None:
        self._stop.set()
        if self._signal_pool is not None:
            self._signal_pool._accepting = False  # noqa: SLF001 — fast STOP
        if self._outcome_pool is not None:
            self._outcome_pool._accepting = False  # noqa: SLF001
        if self._trade_buffer is not None:
            self._trade_buffer._stop.set()
        if self._ws:
            self._ws.request_stop()

    def _sync_signal_metrics(self) -> None:
        if self._signal_pool is not None:
            self.health.apply_signal_pool_metrics(self._signal_pool.metrics_dict())

    def _ensure_signal_pool(self) -> SignalWorkerPool | None:
        if not self.enable_signals or not self.signal_symbols:
            return None
        if self._signal_pool is None:
            catchup = build_live_catchup_callable(
                ch_settings=self._ch_settings,
                candles=self.repo,
                signals=self.signals_repo,
                state=self.state_repo,
                run_signal_catchup=run_signal_catchup,
            )
            self._signal_pool = SignalWorkerPool(
                symbols=self.signal_symbols,
                catchup_fn=catchup,
                health=self.health,
                workers=self._signal_workers,
                queue_maxsize=self._signal_queue_maxsize,
                shutdown_drain_s=self._signal_shutdown_drain_s,
            )
            self.health._signal_metrics_provider = self._signal_pool.metrics_dict
        return self._signal_pool

    def _ensure_outcome_pool(self) -> OutcomeWorkerPool | None:
        if not self.enable_signals or not self.signal_symbols:
            return None
        if self._outcome_pool is None:
            self._outcome_pool = OutcomeWorkerPool(
                symbols=self.signal_symbols,
                eval_fn=build_live_outcome_callable(ch_settings=self._ch_settings),
                workers=2,
            )
        return self._outcome_pool

    async def _start_signal_pool(self) -> None:
        pool = self._ensure_signal_pool()
        if pool is not None and not pool._worker_tasks:  # noqa: SLF001
            pool.start()
            self._sync_signal_metrics()
        op = self._ensure_outcome_pool()
        if op is not None and not op._worker_tasks:  # noqa: SLF001
            op.start()

    async def _stop_signal_pool(self) -> None:
        if self._signal_pool is not None:
            await self._signal_pool.stop(drain_s=self._signal_shutdown_drain_s)
            self._sync_signal_metrics()
        if self._outcome_pool is not None:
            await self._outcome_pool.stop(drain_s=3.0)

    async def _insert_closed(self, candle: Candle1m) -> None:
        async with self._insert_lock:
            await asyncio.to_thread(self.buffer.add, candle)
        ot = ensure_utc(candle.open_time)
        self.health.last_closed_candle_by_symbol[candle.symbol] = ot
        sh = self.health.ensure_symbol(candle.symbol)
        sh.last_closed_candle_at = ot
        sh.last_persisted_open_time = ot
        sh.last_websocket_update_at = datetime.now(timezone.utc)
        if sh.state == SymbolRuntimeState.STALE:
            sh.state = SymbolRuntimeState.LIVE
        self.health.symbols_stale.discard(candle.symbol)
        logger.info(
            "LIVE_CLOSED %s open_time=%s source=%s",
            candle.symbol,
            ot.isoformat(),
            candle.source,
        )
        # LIVE path: never await signal catch-up on the WS recv chain
        if self.enable_signals and candle.symbol in self._signal_symbol_set and htf_boundary_at_close(candle.close_time):
            pool = self._ensure_signal_pool()
            if pool is not None:
                pool.enqueue(
                    candle.symbol,
                    signal_catchup_end_exclusive(candle.close_time),
                    reason=REASON_LIVE_HTF_BOUNDARY,
                )
                self._sync_signal_metrics()
        # Every closed 1m: non-blocking BE50 outcome tick (OPEN trades only)
        if self.enable_signals and candle.symbol in self._signal_symbol_set:
            op = self._ensure_outcome_pool()
            if op is not None:
                op.enqueue(candle.symbol, ensure_utc(candle.close_time))

    async def _on_public_trade(self, trade) -> None:
        """Enqueue live public trade; never raise into the kline WS path."""
        buf = self._trade_buffer
        if buf is None:
            return
        try:
            buf.enqueue(trade)
            self.health.apply_public_trade_metrics(buf.metrics.to_dict())
        except Exception as exc:  # noqa: BLE001
            logger.exception("public trade enqueue failed: %s", exc)
            self.health.public_trade_last_error = str(exc)[:300]

    def _ensure_trade_buffer(self):
        if not self.enable_public_trades or not self.public_trade_symbols:
            return None
        if self._trade_buffer is None:
            from signal_generator.bybit.live.trade_buffer import PublicTradeInsertBuffer

            repo = CanonicalPublicTradeRepository(self.ch)
            self._trade_buffer = PublicTradeInsertBuffer(
                repo,
                queue_maxsize=self._public_trade_queue_maxsize,
                batch_size=self._public_trade_batch_size,
            )
        return self._trade_buffer

    async def _catchup_symbol_blocking(self, symbol: str, *, end: datetime) -> None:
        """Startup/reconnect/stale blocking catch-up (allowed to gate LIVE)."""
        sh = self.health.ensure_symbol(symbol)
        sh.signal_processor_state = "CATCHING_UP"
        try:
            async with self._signal_lock:
                metrics = await asyncio.to_thread(
                    run_signal_catchup,
                    symbols=[symbol],
                    candles=self.repo,
                    signals=self.signals_repo,
                    state=self.state_repo,
                    end=end,
                )
            sh.signal_processor_state = "CAUGHT_UP"
            sh.signal_processing_lag_seconds = 0.0
            sh.signal_last_processed_at = datetime.now(timezone.utc)
            if metrics.errors:
                sh.signal_processor_state = "DEGRADED"
                sh.signal_last_error = f"signal_catchup_errors={metrics.errors}"
                sh.last_error = sh.signal_last_error
        except Exception as exc:  # noqa: BLE001
            logger.exception("signal catch-up failed for %s: %s", symbol, exc)
            sh.signal_processor_state = "DEGRADED"
            sh.signal_last_error = str(exc)
            sh.last_error = str(exc)

    async def _catchup_all_blocking(self, *, end: datetime) -> None:
        for symbol in self.signal_symbols:
            if self._stop.is_set():
                return
            sh = self.health.ensure_symbol(symbol)
            sh.signal_processor_state = "CATCHING_UP"
            await self._catchup_symbol_blocking(symbol, end=end)

    async def _post_recovery_continuity(self) -> None:
        """Fresh as_of pass after slow signal catch-up (lost-minute during wait)."""
        if not self.repair_internal:
            return
        as_of = datetime.now(timezone.utc)

        def _run() -> int:
            inserted = 0
            for symbol in self.symbols:
                # Trailing tip first
                recover_symbols(
                    [symbol],
                    repo=self.repo,
                    history=self.history,
                    as_of=as_of,
                )
                _n, ins = repair_recent_continuity(
                    symbol=symbol,
                    ch=self.ch,
                    repo=self.repo,
                    history=self.history,
                    as_of=as_of,
                    lookback_minutes=self.recent_continuity_minutes,
                )
                inserted += ins
            return inserted

        ins = await asyncio.to_thread(_run)
        if ins:
            self.health.recovery_candles_inserted += ins
            logger.info("POST_RECOVERY_CONTINUITY inserted=%s", ins)

    async def run_recovery(self, *, reason: str) -> bool:
        self.health.set_state(CollectorState.RECOVERING, reason=reason)
        for symbol in self.symbols:
            sh = self.health.ensure_symbol(symbol)
            sh.state = SymbolRuntimeState.RECOVERING
            sh.recovery_state = "RUNNING"
        self.health.symbols_recovering = len(self.symbols)
        self.health.symbols_live = 0

        results = await asyncio.to_thread(
            recover_symbols_full,
            self.symbols,
            ch=self.ch,
            repo=self.repo,
            history=self.history,
            repair_internal=self.repair_internal,
            internal_lookback_days=self.internal_lookback_days,
            recent_continuity_minutes=self.recent_continuity_minutes,
        )
        self.health.recovery_run_count += 1
        self.health.last_recovery_at = datetime.now(timezone.utc)
        gaps = sum(1 for r in results if r.gap_minutes > 0 or r.internal_ranges_repaired > 0)
        inserted = sum(r.inserted for r in results)
        self.health.recovery_gap_count += gaps
        self.health.recovery_candles_inserted += inserted
        failed = [r for r in results if not r.ok]
        for r in results:
            sh = self.health.ensure_symbol(r.symbol)
            sh.recovery_candles_inserted += r.inserted
            if r.ok:
                sh.recovery_state = "DONE"
                last = self.repo.get_last_closed_open_time(r.symbol)
                if last:
                    self.health.last_closed_candle_by_symbol[r.symbol] = last
                    sh.last_persisted_open_time = last
                    sh.last_closed_candle_at = last
            else:
                sh.recovery_state = "ERROR"
                sh.state = SymbolRuntimeState.ERROR
                sh.last_error = r.error
        self.health.symbols_recovering = 0
        if failed:
            self.health.last_error = "; ".join(
                f"{r.symbol}:{r.error}" for r in failed
            )
            self.health.set_state(
                CollectorState.DEGRADED, reason="recovery_partial_failure"
            )
            return False

        # Signal catch-up must not block WebSocket connect.
        # New strategy versions (empty watermarks) otherwise stall LIVE for hours.
        # Catch-up continues via SignalWorkerPool in parallel with live candles.
        if self.enable_signals:
            end = signal_catchup_end_exclusive(
                last_fully_closed_open_time() + timedelta(minutes=1)
            )
            await self._start_signal_pool()
            pool = self._ensure_signal_pool()
            if pool is not None:
                for symbol in self.signal_symbols:
                    sh = self.health.ensure_symbol(symbol)
                    sh.signal_processor_state = "CATCHING_UP"
                    pool.enqueue(symbol, end)
                logger.info(
                    "SIGNAL_CATCHUP_ENQUEUED symbols=%s end=%s (non-blocking)",
                    len(self.signal_symbols),
                    end.isoformat(),
                )

        # Tip repair for minutes lost during candle recovery (not full signal catch-up)
        await self._post_recovery_continuity()

        return True

    async def _stale_symbol_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30.0)
                return
            except asyncio.TimeoutError:
                pass
            if self.health.state != CollectorState.LIVE:
                continue
            now = datetime.now(timezone.utc)
            threshold = timedelta(minutes=self.stale_symbol_minutes)
            for symbol in self.symbols:
                last = self.health.last_closed_candle_by_symbol.get(symbol)
                if last is None:
                    last = await asyncio.to_thread(
                        self.repo.get_last_closed_open_time, symbol
                    )
                    if last:
                        self.health.last_closed_candle_by_symbol[symbol] = last
                if last is None:
                    continue
                age = now - ensure_utc(last) - timedelta(minutes=1)
                sh = self.health.ensure_symbol(symbol)
                if age > threshold:
                    sh.state = SymbolRuntimeState.STALE
                    self.health.symbols_stale.add(symbol)
                    logger.warning(
                        "STALE_SYMBOL %s last_closed=%s age_after_close=%s",
                        symbol,
                        ensure_utc(last).isoformat(),
                        age,
                    )
                    await asyncio.to_thread(
                        recover_symbols,
                        [symbol],
                        repo=self.repo,
                        history=self.history,
                    )
                    await asyncio.to_thread(
                        partial(
                            repair_recent_continuity,
                            symbol=symbol,
                            ch=self.ch,
                            repo=self.repo,
                            history=self.history,
                            lookback_minutes=self.recent_continuity_minutes,
                        )
                    )
                    if self.enable_signals and symbol in self._signal_symbol_set:
                        end = signal_catchup_end_exclusive(
                            last_fully_closed_open_time() + timedelta(minutes=1)
                        )
                        pool = self._ensure_signal_pool()
                        if pool is not None and pool.accepting:
                            pool.enqueue(symbol, end, reason=REASON_STALE_RECOVERY)
                        else:
                            await self._catchup_symbol_blocking(symbol, end=end)
                    refreshed = await asyncio.to_thread(
                        self.repo.get_last_closed_open_time, symbol
                    )
                    if refreshed:
                        self.health.last_closed_candle_by_symbol[symbol] = refreshed
                        sh.last_persisted_open_time = refreshed
                        age2 = now - ensure_utc(refreshed) - timedelta(minutes=1)
                        if age2 <= threshold:
                            self.health.symbols_stale.discard(symbol)
                            sh.state = SymbolRuntimeState.LIVE
            self._sync_signal_metrics()

    async def run(self) -> None:
        self.health.started_at = datetime.now(timezone.utc)
        self.health.set_state(CollectorState.STARTING)
        backoff_idx = 0

        while not self._stop.is_set():
            ok = await self.run_recovery(reason="startup_or_reconnect")
            if self._stop.is_set():
                break
            if not ok:
                delay = BACKOFF_SCHEDULE_S[min(backoff_idx, len(BACKOFF_SCHEDULE_S) - 1)]
                backoff_idx = min(backoff_idx + 1, len(BACKOFF_SCHEDULE_S) - 1)
                self.health.consecutive_reconnect_failures += 1
                logger.error("recovery failed; retry in %ss", delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    break
                except asyncio.TimeoutError:
                    continue

            self.health.set_state(CollectorState.CONNECTING, reason="recovery_ok")
            for symbol in self.symbols:
                sh = self.health.ensure_symbol(symbol)
                if sh.state != SymbolRuntimeState.ERROR:
                    sh.state = SymbolRuntimeState.SUBSCRIBING
            self.health.set_state(CollectorState.SUBSCRIBING, reason="ws_subscribe")
            backoff_idx = 0

            await self._start_signal_pool()

            trade_buf = self._ensure_trade_buffer()
            if trade_buf is not None:
                trade_buf.start()
                trade_buf.note_reconnect()
                self.health.apply_public_trade_metrics(trade_buf.metrics.to_dict())

            self._ws = self.ws_factory(
                self.symbols,
                on_closed_candle=self._insert_closed,
                on_public_trade=self._on_public_trade if trade_buf is not None else None,
                public_trade_symbols=self.public_trade_symbols or None,
                on_event=self._on_ws_event,
            )
            stale_task = asyncio.create_task(self._stale_symbol_loop())
            try:
                await self._ws.run_forever()
            except Exception as exc:  # noqa: BLE001
                self.health.last_error = str(exc)
                logger.exception("websocket session error: %s", exc)
            finally:
                self.health.note_disconnected()
                stale_task.cancel()
                try:
                    await stale_task
                except asyncio.CancelledError:
                    pass
                self._ws = None

            if self._stop.is_set():
                break

            self.health.reconnect_count += 1
            self.health.consecutive_reconnect_failures += 1
            delay = BACKOFF_SCHEDULE_S[min(backoff_idx, len(BACKOFF_SCHEDULE_S) - 1)]
            backoff_idx = min(backoff_idx + 1, len(BACKOFF_SCHEDULE_S) - 1)
            self.health.set_state(
                CollectorState.RECONNECTING, reason=f"backoff={delay}s"
            )
            self.health.symbols_live = 0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                continue

        await self._graceful_shutdown()

    async def _graceful_shutdown(self) -> None:
        self.health.set_state(CollectorState.STOPPING)
        for symbol in self.symbols:
            sh = self.health.ensure_symbol(symbol)
            sh.state = SymbolRuntimeState.STOPPED
        await self._stop_signal_pool()
        await asyncio.to_thread(self.buffer.flush)
        if self._trade_buffer is not None:
            await self._trade_buffer.stop()
            self.health.apply_public_trade_metrics(self._trade_buffer.metrics.to_dict())
        # Final blocking catch-up — watermark SoT; short because STOP must stay snappy
        if self.enable_signals:
            try:
                end = signal_catchup_end_exclusive(
                    last_fully_closed_open_time() + timedelta(minutes=1)
                )
                # Prefer pool dirty flush via blocking catch-up once
                await self._catchup_all_blocking(end=end)
            except Exception as exc:  # noqa: BLE001
                logger.exception("shutdown signal catch-up failed: %s", exc)
        self._sync_signal_metrics()
        self.health.set_state(CollectorState.STOPPED)

    async def _on_ws_event(self, name: str, data: dict) -> None:
        if name == "connected":
            self.health.note_connected()
        elif name == "pong":
            self.health.note_pong()
        elif name == "ping_sent":
            self.health.note_ping()
        elif name == "subscribe_sent":
            self.health.note_message()
            for symbol in self.symbols:
                sh = self.health.ensure_symbol(symbol)
                sh.state = SymbolRuntimeState.SUBSCRIBING
        elif name == "subscribe_ack":
            self.health.note_message()
            success = bool(data.get("success"))
            ack_args = data.get("args") or []
            ack_symbols = [
                str(a).rsplit(".", 1)[-1].upper() for a in ack_args if a
            ]
            if success:
                targets = ack_symbols or list(self.candle_symbols)
                for symbol in targets:
                    self.health.mark_subscribed(symbol, acked=True)
                if len(self.health.subscribed_symbols) >= len(self.candle_symbols):
                    self.health.set_state(CollectorState.LIVE, reason="subscribe_acked")
                    for symbol in self.candle_symbols:
                        self.health.mark_symbol_live(symbol)
                    self.health.symbols_live = len(self.candle_symbols)
            else:
                self.health.last_error = str(data.get("ret_msg") or "subscribe_failed")
                self.health.set_state(CollectorState.DEGRADED, reason="subscribe_nack")
        elif name == "topic_message":
            self.health.note_message()
            symbol = str(data.get("symbol") or "").upper()
            if symbol:
                sh = self.health.ensure_symbol(symbol)
                sh.last_websocket_update_at = datetime.now(timezone.utc)
                if not sh.subscribed:
                    self.health.mark_subscribed(symbol, acked=True)
                if (
                    self.health.state == CollectorState.SUBSCRIBING
                    and len(self.health.subscribed_symbols) == len(self.symbols)
                ):
                    self.health.set_state(CollectorState.LIVE, reason="topics_active")
                    for s in self.symbols:
                        self.health.mark_symbol_live(s)
                    self.health.symbols_live = len(self.symbols)
        elif name == "unconfirmed":
            self.health.note_message()
            symbol = str(data.get("symbol") or "").upper()
            if symbol:
                sh = self.health.ensure_symbol(symbol)
                sh.last_websocket_update_at = datetime.now(timezone.utc)
                if data.get("time") is not None:
                    self.health.set_forming_candle(
                        symbol,
                        {
                            "time": int(data["time"]),
                            "open": data.get("open"),
                            "high": data.get("high"),
                            "low": data.get("low"),
                            "close": data.get("close"),
                            "volume": data.get("volume"),
                            "confirm": False,
                        },
                    )
        elif name in ("other", "subscribed"):
            self.health.note_message()
        elif name == "stale":
            self.health.set_state(CollectorState.RECONNECTING, reason="ws_stale")
        self._sync_signal_metrics()


def install_signal_handlers(
    collector: Live1mCollector, loop: asyncio.AbstractEventLoop
) -> None:
    def _handler() -> None:
        logger.info("signal received → graceful stop")
        collector.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:  # pragma: no cover - Windows
            signal.signal(sig, lambda *_: collector.request_stop())


def acquire_singleton_lock(path: Path) -> Path:
    """Prevent a second collector instance (exclusive lock file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl

    fd = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fd.close()
        raise RuntimeError(f"another collector instance holds {path}") from exc
    fd.seek(0)
    fd.truncate()
    fd.write(f"pid={__import__('os').getpid()}\n")
    fd.flush()
    acquire_singleton_lock._fd = fd  # type: ignore[attr-defined]
    return path
