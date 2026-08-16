"""Non-blocking outcome evaluation pool (Frozen BE50).

Woken from the live collector after each closed 1m candle. Never runs on the
WebSocket recv thread — workers use ``run_in_executor`` with per-thread CH clients.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Sequence

from signal_generator.bybit.history import ensure_utc

logger = logging.getLogger(__name__)

DEFAULT_OUTCOME_WORKERS = 2
DEFAULT_OUTCOME_QUEUE_MAXSIZE = 500


class OutcomeWorkerPool:
    """Coalescing per-symbol outcome evaluator (OPEN trades only)."""

    def __init__(
        self,
        *,
        symbols: Sequence[str],
        eval_fn: Callable[..., object],
        workers: int = DEFAULT_OUTCOME_WORKERS,
        queue_maxsize: int = DEFAULT_OUTCOME_QUEUE_MAXSIZE,
    ) -> None:
        self.symbols = [s.upper() for s in symbols]
        self._eval_fn = eval_fn
        self.workers = max(1, workers)
        self.queue_maxsize = max(1, queue_maxsize)
        self._wake: asyncio.Queue[str | None] = asyncio.Queue(maxsize=queue_maxsize)
        self._target_as_of: dict[str, datetime] = {}
        self._inflight: set[str] = set()
        self._wake_pending: set[str] = set()
        self._dirty: set[str] = set()
        self._accepting = False
        self._stop = asyncio.Event()
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._executor = ThreadPoolExecutor(
            max_workers=self.workers,
            thread_name_prefix="outcome-worker",
        )
        self.jobs_processed = 0
        self.jobs_failed = 0

    def start(self) -> None:
        if self._worker_tasks:
            return
        self._stop.clear()
        self._accepting = True
        loop = asyncio.get_running_loop()
        for i in range(self.workers):
            self._worker_tasks.append(loop.create_task(self._worker_loop(i)))

    async def stop(self, drain_s: float = 3.0) -> None:
        self._accepting = False
        self._stop.set()
        for _ in self._worker_tasks:
            try:
                self._wake.put_nowait(None)
            except asyncio.QueueFull:
                break
        if self._worker_tasks:
            await asyncio.wait(self._worker_tasks, timeout=drain_s)
        self._worker_tasks.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def enqueue(self, symbol: str, as_of: datetime) -> None:
        if not self._accepting:
            return
        symbol = symbol.upper()
        as_of = ensure_utc(as_of)
        prev = self._target_as_of.get(symbol)
        if prev is None or as_of > prev:
            self._target_as_of[symbol] = as_of
        if symbol in self._wake_pending or symbol in self._inflight:
            return
        try:
            self._wake.put_nowait(symbol)
            self._wake_pending.add(symbol)
        except asyncio.QueueFull:
            self._dirty.add(symbol)

    async def _worker_loop(self, worker_id: int) -> None:
        while not self._stop.is_set():
            try:
                symbol = await asyncio.wait_for(self._wake.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # flush dirty
                if self._dirty:
                    sym = self._dirty.pop()
                    from datetime import timezone as _tz

                    self.enqueue(
                        sym,
                        self._target_as_of.get(sym)
                        or datetime.now(_tz.utc),
                    )
                continue
            if symbol is None:
                break
            self._wake_pending.discard(symbol)
            as_of = self._target_as_of.pop(symbol, None)
            if as_of is None:
                continue
            self._inflight.add(symbol)
            try:
                loop = asyncio.get_running_loop()
                t0 = time.perf_counter()
                await loop.run_in_executor(
                    self._executor,
                    lambda s=symbol, a=as_of: self._eval_fn(symbol=s, as_of=a),
                )
                self.jobs_processed += 1
                logger.debug(
                    "OUTCOME_OK worker=%s symbol=%s ms=%.1f",
                    worker_id,
                    symbol,
                    (time.perf_counter() - t0) * 1000.0,
                )
            except Exception:  # noqa: BLE001
                self.jobs_failed += 1
                self._dirty.add(symbol)
                logger.exception("outcome eval failed symbol=%s", symbol)
            finally:
                self._inflight.discard(symbol)
                # re-wake if newer as_of arrived while inflight
                if symbol in self._target_as_of:
                    self.enqueue(symbol, self._target_as_of[symbol])


def build_live_outcome_callable(*, ch_settings: object | None = None) -> Callable[..., object]:
    import threading

    from signal_generator.config import ClickHouseSettings, get_clickhouse_settings
    from signal_generator.db.candles import CandleRepository
    from signal_generator.db.client import ClickHouseClient
    from signal_generator.db.outcomes import SignalOutcomeRepository
    from signal_generator.db.signals import SignalRepository
    from signal_generator.pipeline.outcome_eval import OutcomeEvaluator

    local = threading.local()

    def _fn(*, symbol: str, as_of: datetime) -> object:
        client = getattr(local, "ch", None)
        if client is None:
            s = (
                ch_settings
                if isinstance(ch_settings, ClickHouseSettings)
                else get_clickhouse_settings()
            )
            client = ClickHouseClient.from_settings(s)
            local.ch = client
            local.ev = OutcomeEvaluator(
                candles=CandleRepository(client),
                signals=SignalRepository(client),
                outcomes=SignalOutcomeRepository(client),
            )
        return local.ev.evaluate_symbol(symbol, as_of=as_of, lookback_hours=168)

    return _fn
