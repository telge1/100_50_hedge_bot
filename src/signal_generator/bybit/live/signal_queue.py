"""Bounded live signal worker pool — decouples Wave-Fade catch-up from WS recv.

Design notes
------------
* Coalescing state lives in ``_target_end`` (bounded by symbol count), not as
  one Queue item per HTF event. ``asyncio.Queue`` only carries wake tokens.
* ``SIGNAL_QUEUE_MAXSIZE`` defaults to 1000: far above live-universe size, so
  wake tokens rarely block; overflow never awaits in the WS path — the symbol
  is marked dirty and re-woken by the scheduler tick / worker completion.
* Persistent watermarks remain SoT; an in-memory queue loss is recovered on
  the next startup/reconnect catch-up.
* Per-symbol exclusivity: at most one ``run_signal_catchup`` in flight per
  symbol; other symbols run in parallel across workers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Sequence

from signal_generator.bybit.history import ensure_utc
from signal_generator.pipeline.versions import STRATEGY_VERSION

logger = logging.getLogger(__name__)

DEFAULT_SIGNAL_WORKERS = 4
DEFAULT_SIGNAL_QUEUE_MAXSIZE = 1000
DEFAULT_SHUTDOWN_DRAIN_S = 5.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_SCHEDULER_TICK_S = 2.0
REASON_LIVE_HTF_BOUNDARY = "LIVE_HTF_BOUNDARY"
REASON_DIRTY_FLUSH = "DIRTY_FLUSH"
REASON_STALE_RECOVERY = "STALE_RECOVERY"


CatchupFn = Callable[..., object]  # sync run_signal_catchup-compatible


@dataclass(slots=True)
class SignalEnqueueResult:
    accepted: bool
    coalesced: bool
    overflow: bool
    dirty: bool
    target_end: datetime | None


class SignalWorkerPool:
    """Async worker pool with coalescing + per-symbol exclusivity."""

    def __init__(
        self,
        *,
        symbols: Sequence[str],
        catchup_fn: CatchupFn,
        health: object | None = None,
        workers: int = DEFAULT_SIGNAL_WORKERS,
        queue_maxsize: int = DEFAULT_SIGNAL_QUEUE_MAXSIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        shutdown_drain_s: float = DEFAULT_SHUTDOWN_DRAIN_S,
        scheduler_tick_s: float = DEFAULT_SCHEDULER_TICK_S,
        strategy_version: str = STRATEGY_VERSION,
        on_symbol_state: Callable[[str, str, dict], None] | None = None,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        if queue_maxsize < 1:
            raise ValueError("queue_maxsize must be >= 1")
        self.symbols = [s.upper() for s in symbols]
        self._catchup_fn = catchup_fn
        self.health = health
        self.workers = workers
        self.queue_maxsize = queue_maxsize
        self.max_retries = max_retries
        self.shutdown_drain_s = shutdown_drain_s
        self.scheduler_tick_s = scheduler_tick_s
        self.strategy_version = strategy_version
        self._on_symbol_state = on_symbol_state

        self._wake: asyncio.Queue[str | None] = asyncio.Queue(maxsize=queue_maxsize)
        self._target_end: dict[str, datetime] = {}
        self._enqueued_at: dict[str, datetime] = {}
        self._inflight: set[str] = set()
        self._wake_pending: set[str] = set()
        self._dirty: set[str] = set()
        self._accepting = False
        self._stop = asyncio.Event()
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._tick_task: asyncio.Task[None] | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="signal-worker",
        )
        self._busy = 0
        self._lock = asyncio.Lock()

        # metrics
        self.jobs_processed = 0
        self.jobs_failed = 0
        self.jobs_retried = 0
        self.overflow_count = 0
        self.processing_last_ms: float | None = None
        self.processing_max_ms: float = 0.0
        self._oldest_enqueued_at: datetime | None = None

    @property
    def accepting(self) -> bool:
        return self._accepting

    def start(self) -> None:
        """Start worker tasks on the running event loop."""
        if self._worker_tasks:
            return
        self._stop.clear()
        self._accepting = True
        # Recreate executor if previous stop() shut it down
        if getattr(self._executor, "_shutdown", False):
            self._executor = ThreadPoolExecutor(
                max_workers=self.workers,
                thread_name_prefix="signal-worker",
            )
        loop = asyncio.get_running_loop()
        for i in range(self.workers):
            self._worker_tasks.append(loop.create_task(self._worker_loop(i)))
        self._tick_task = loop.create_task(self._scheduler_tick())
        logger.info(
            "SIGNAL_POOL started workers=%s queue_maxsize=%s",
            self.workers,
            self.queue_maxsize,
        )

    async def stop(self, *, drain_s: float | None = None) -> None:
        """Stop accepting; brief drain; cancel workers. Watermark recovers remainder."""
        drain = self.shutdown_drain_s if drain_s is None else drain_s
        self._accepting = False
        # Brief attempt to finish in-flight / pending wakes
        deadline = time.monotonic() + max(0.0, drain)
        while time.monotonic() < deadline:
            async with self._lock:
                pending = bool(self._target_end) or bool(self._inflight) or not self._wake.empty()
            if not pending:
                break
            await asyncio.sleep(0.05)
        self._stop.set()
        # Unblock workers
        for _ in self._worker_tasks:
            try:
                self._wake.put_nowait(None)
            except asyncio.QueueFull:
                break
        tasks = list(self._worker_tasks)
        if self._tick_task:
            tasks.append(self._tick_task)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._worker_tasks.clear()
        self._tick_task = None
        self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info(
            "SIGNAL_POOL stopped processed=%s failed=%s dirty=%s",
            self.jobs_processed,
            self.jobs_failed,
            sorted(self._dirty),
        )

    def enqueue(
        self,
        symbol: str,
        end: datetime,
        *,
        reason: str = REASON_LIVE_HTF_BOUNDARY,
    ) -> SignalEnqueueResult:
        """Non-blocking enqueue with coalescing. Never awaits."""
        symbol = symbol.upper()
        end = ensure_utc(end)
        if not self._accepting:
            self._dirty.add(symbol)
            self._target_end[symbol] = max(end, self._target_end.get(symbol, end))
            self._note_symbol(symbol, "QUEUED", dirty=True, reason="not_accepting")
            return SignalEnqueueResult(
                accepted=False,
                coalesced=False,
                overflow=False,
                dirty=True,
                target_end=self._target_end.get(symbol),
            )

        coalesced = False
        prev = self._target_end.get(symbol)
        if prev is not None:
            if end <= prev:
                coalesced = True
                self._note_symbol(symbol, "QUEUED", coalesced=True)
                return SignalEnqueueResult(
                    accepted=True,
                    coalesced=True,
                    overflow=False,
                    dirty=symbol in self._dirty,
                    target_end=prev,
                )
            coalesced = True
        self._target_end[symbol] = end
        now = datetime.now(timezone.utc)
        if symbol not in self._enqueued_at:
            self._enqueued_at[symbol] = now
        self._refresh_oldest()

        overflow = False
        woken = self._try_wake(symbol)
        if not woken and symbol not in self._inflight:
            # Wake queue full — keep target_end, mark dirty for tick retry
            overflow = True
            self.overflow_count += 1
            self._dirty.add(symbol)
            logger.warning(
                "SIGNAL_QUEUE overflow symbol=%s end=%s (dirty+coalesce; WS continues)",
                symbol,
                end.isoformat(),
            )
        self._note_symbol(
            symbol,
            "QUEUED",
            coalesced=coalesced,
            overflow=overflow,
            reason=reason,
        )
        return SignalEnqueueResult(
            accepted=True,
            coalesced=coalesced,
            overflow=overflow,
            dirty=symbol in self._dirty,
            target_end=end,
        )

    def mark_dirty(self, symbol: str, *, end: datetime | None = None) -> None:
        symbol = symbol.upper()
        self._dirty.add(symbol)
        if end is not None:
            end = ensure_utc(end)
            self._target_end[symbol] = max(end, self._target_end.get(symbol, end))
            if symbol not in self._enqueued_at:
                self._enqueued_at[symbol] = datetime.now(timezone.utc)
        self._try_wake(symbol)

    def metrics_dict(self) -> dict:
        oldest_age = None
        if self._oldest_enqueued_at is not None:
            oldest_age = (
                datetime.now(timezone.utc) - self._oldest_enqueued_at
            ).total_seconds()
        lag = None
        if self.health is not None:
            lag = getattr(self.health, "max_signal_lag_seconds", None)
        return {
            "signal_queue_depth": self.queue_depth,
            "signal_queue_maxsize": self.queue_maxsize,
            "signal_oldest_job_age_seconds": oldest_age,
            "signal_workers_total": self.workers,
            "signal_workers_busy": self._busy,
            "signal_jobs_processed": self.jobs_processed,
            "signal_jobs_failed": self.jobs_failed,
            "signal_jobs_retried": self.jobs_retried,
            "signal_processing_last_ms": self.processing_last_ms,
            "signal_processing_max_ms": self.processing_max_ms,
            "max_signal_lag_seconds": lag,
            "signal_dirty_symbols": sorted(self._dirty),
            "signal_overflow_count": self.overflow_count,
            "signal_pending_symbols": sorted(self._target_end),
            "signal_inflight_symbols": sorted(self._inflight),
        }

    @property
    def queue_depth(self) -> int:
        # Logical pending work: targets not yet completed + wake qsize
        return len(self._target_end) + self._wake.qsize()

    def _try_wake(self, symbol: str) -> bool:
        if symbol in self._wake_pending or symbol in self._inflight:
            return True
        try:
            self._wake.put_nowait(symbol)
            self._wake_pending.add(symbol)
            return True
        except asyncio.QueueFull:
            return False

    def _refresh_oldest(self) -> None:
        if not self._enqueued_at:
            self._oldest_enqueued_at = None
            return
        self._oldest_enqueued_at = min(self._enqueued_at.values())

    def _note_symbol(self, symbol: str, state: str, **_meta: object) -> None:
        if self._on_symbol_state:
            self._on_symbol_state(symbol, state, dict(_meta))
        if self.health is not None:
            ensure = getattr(self.health, "ensure_symbol", None)
            if ensure:
                sh = ensure(symbol)
                sh.signal_processor_state = state

    async def _scheduler_tick(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.scheduler_tick_s)
                return
            except asyncio.TimeoutError:
                pass
            # Re-wake pending targets not inflight; dirty symbols kept for recovery
            for symbol in list(self._target_end.keys()):
                if symbol not in self._inflight and symbol not in self._dirty:
                    self._try_wake(symbol)
            # Slow retry for dirty (failed) symbols
            for symbol in list(self._dirty):
                if symbol not in self._inflight:
                    self._try_wake(symbol)

    async def _worker_loop(self, worker_id: int) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                symbol = await self._wake.get()
            except asyncio.CancelledError:
                raise
            if symbol is None:
                return
            self._wake_pending.discard(symbol)
            if symbol in self._inflight:
                continue
            async with self._lock:
                end = self._target_end.get(symbol)
                if end is None:
                    continue
                # Claim: leave target until success so concurrent enqueue can raise end
                self._inflight.add(symbol)
                claimed_end = end
            self._busy += 1
            self._note_symbol(symbol, "PROCESSING")
            ok = False
            last_exc: Exception | None = None
            for attempt in range(1, self.max_retries + 1):
                if self._stop.is_set() and not self._accepting:
                    break
                # Re-read coalesced end (may have advanced)
                async with self._lock:
                    claimed_end = self._target_end.get(symbol, claimed_end)
                t0 = time.perf_counter()
                try:
                    await loop.run_in_executor(
                        self._executor,
                        self._invoke_catchup,
                        symbol,
                        claimed_end,
                    )
                    ms = (time.perf_counter() - t0) * 1000.0
                    self.processing_last_ms = ms
                    self.processing_max_ms = max(self.processing_max_ms, ms)
                    ok = True
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    self.jobs_retried += 1
                    logger.exception(
                        "SIGNAL_WORKER %s failed symbol=%s attempt=%s/%s: %s",
                        worker_id,
                        symbol,
                        attempt,
                        self.max_retries,
                        exc,
                    )
                    self._note_symbol(symbol, "DEGRADED", error=str(exc))
                    await asyncio.sleep(min(2 ** (attempt - 1), 8))
            self._busy = max(0, self._busy - 1)
            async with self._lock:
                self._inflight.discard(symbol)
                if ok:
                    self.jobs_processed += 1
                    # Clear target only if not advanced past claimed
                    cur = self._target_end.get(symbol)
                    if cur is not None and cur <= claimed_end:
                        self._target_end.pop(symbol, None)
                        self._enqueued_at.pop(symbol, None)
                        self._dirty.discard(symbol)
                        self._note_symbol(symbol, "CAUGHT_UP")
                        if self.health is not None:
                            sh = self.health.ensure_symbol(symbol)
                            sh.signal_last_processed_at = datetime.now(timezone.utc)
                            sh.signal_processing_lag_seconds = 0.0
                            sh.signal_last_error = None
                    else:
                        # More work coalesced while running — re-wake
                        self._try_wake(symbol)
                        self._note_symbol(symbol, "QUEUED")
                else:
                    self.jobs_failed += 1
                    self._dirty.add(symbol)
                    # Keep target_end for watermark/restart recovery; do not
                    # hot-loop retries — scheduler tick will re-wake slowly.
                    err = str(last_exc) if last_exc else "unknown"
                    self._note_symbol(symbol, "DEGRADED", error=err)
                    if self.health is not None:
                        sh = self.health.ensure_symbol(symbol)
                        sh.signal_last_error = err
                        sh.signal_processor_state = "DEGRADED"
                self._refresh_oldest()

    def _invoke_catchup(self, symbol: str, end: datetime) -> None:
        self._catchup_fn(symbol=symbol, end=end)


def build_live_catchup_callable(
    *,
    ch_settings: object | None = None,
    candles=None,
    signals=None,
    state=None,
    run_signal_catchup: Callable[..., object],
) -> CatchupFn:
    """Build catch-up fn with per-thread ClickHouse clients (CH is not thread-safe)."""
    import threading

    from signal_generator.config import get_clickhouse_settings
    from signal_generator.db.candles import CandleRepository
    from signal_generator.db.client import ClickHouseClient
    from signal_generator.db.processing_state import ProcessingStateRepository
    from signal_generator.db.signals import SignalRepository

    local = threading.local()
    settings = ch_settings

    def _fn(*, symbol: str, end: datetime) -> object:
        # Prefer thread-local clients when settings available (parallel workers).
        if settings is not None:
            client = getattr(local, "ch", None)
            if client is None:
                from signal_generator.config import ClickHouseSettings

                s = settings if isinstance(settings, ClickHouseSettings) else get_clickhouse_settings()
                client = ClickHouseClient.from_settings(s)
                local.ch = client
                local.candles = CandleRepository(client)
                local.signals = SignalRepository(client)
                local.state = ProcessingStateRepository(client)
            return run_signal_catchup(
                symbols=[symbol],
                candles=local.candles,
                signals=local.signals,
                state=local.state,
                end=end,
            )
        # Fallback: shared repos (single-threaded / tests)
        return run_signal_catchup(
            symbols=[symbol],
            candles=candles,
            signals=signals,
            state=state,
            end=end,
        )

    return _fn
