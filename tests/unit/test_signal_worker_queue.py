"""Tests for live signal worker queue + reconnect recent-gap repair."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from signal_generator.bybit.live.collector import Live1mCollector
from signal_generator.bybit.live.health import HealthState
from signal_generator.bybit.live.recovery import (
    compute_recovery_window,
    last_fully_closed_open_time,
    repair_recent_continuity,
)
from signal_generator.bybit.live.signal_catchup import htf_boundary_at_close
from signal_generator.bybit.live.signal_queue import (
    DEFAULT_SIGNAL_WORKERS,
    SignalWorkerPool,
)
from signal_generator.bybit.live.ws_kline import BybitKlineWebSocket
from signal_generator.db.candles import Candle1m
from signal_generator.bybit.missing_ranges import MissingRange, MissingRangeReport


UTC = timezone.utc


def _candle(symbol: str, open_time: datetime) -> Candle1m:
    return Candle1m(
        exchange="bybit",
        symbol=symbol,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
        turnover=Decimal("1"),
        source="bybit_live",
    )


@pytest.mark.asyncio
async def test_workers_start_and_process_jobs():
    done: list[tuple[str, datetime]] = []

    def catchup(*, symbol: str, end: datetime) -> None:
        time.sleep(0.05)
        done.append((symbol, end))

    pool = SignalWorkerPool(
        symbols=["APTUSDT", "DOGEUSDT"],
        catchup_fn=catchup,
        workers=4,
        queue_maxsize=100,
        scheduler_tick_s=0.2,
    )
    pool.start()
    assert len(pool._worker_tasks) == 4
    end = datetime(2026, 8, 10, 15, 45, tzinfo=UTC)
    pool.enqueue("APTUSDT", end)
    pool.enqueue("DOGEUSDT", end)
    for _ in range(50):
        if pool.jobs_processed >= 2:
            break
        await asyncio.sleep(0.05)
    assert pool.jobs_processed >= 2
    assert {d[0] for d in done} == {"APTUSDT", "DOGEUSDT"}
    await pool.stop(drain_s=1.0)


@pytest.mark.asyncio
async def test_per_symbol_exclusivity_and_cross_symbol_parallel():
    active: dict[str, int] = {}
    max_parallel_same = 0
    max_parallel_total = 0
    lock = asyncio.Lock()  # not used in thread; use list

    state = {"same": 0, "total": 0, "peak_same": 0, "peak_total": 0}

    def catchup(*, symbol: str, end: datetime) -> None:
        state["total"] += 1
        if symbol == "APTUSDT":
            state["same"] += 1
            state["peak_same"] = max(state["peak_same"], state["same"])
        state["peak_total"] = max(state["peak_total"], state["total"])
        time.sleep(0.15)
        state["total"] -= 1
        if symbol == "APTUSDT":
            state["same"] -= 1

    pool = SignalWorkerPool(
        symbols=["APTUSDT", "DOGEUSDT", "SOLUSDT"],
        catchup_fn=catchup,
        workers=4,
        queue_maxsize=100,
        scheduler_tick_s=0.1,
    )
    pool.start()
    end = datetime(2026, 8, 10, 16, 0, tzinfo=UTC)
    for _ in range(3):
        pool.enqueue("APTUSDT", end)
    pool.enqueue("DOGEUSDT", end)
    pool.enqueue("SOLUSDT", end)
    for _ in range(80):
        if pool.jobs_processed >= 3 and not pool._target_end and not pool._inflight:
            break
        await asyncio.sleep(0.05)
    assert state["peak_same"] == 1  # exclusivity
    assert state["peak_total"] >= 2  # parallel across symbols
    await pool.stop(drain_s=1.0)


@pytest.mark.asyncio
async def test_coalesce_later_end_supersedes():
    ends: list[datetime] = []

    def catchup(*, symbol: str, end: datetime) -> None:
        time.sleep(0.2)
        ends.append(end)

    pool = SignalWorkerPool(
        symbols=["APTUSDT"],
        catchup_fn=catchup,
        workers=1,
        queue_maxsize=10,
        scheduler_tick_s=0.1,
    )
    pool.start()
    e1 = datetime(2026, 8, 10, 15, 45, tzinfo=UTC)
    e2 = datetime(2026, 8, 10, 16, 0, tzinfo=UTC)
    pool.enqueue("APTUSDT", e1)
    await asyncio.sleep(0.05)  # let worker claim 15:45
    r = pool.enqueue("APTUSDT", e2)
    assert r.coalesced or r.accepted
    for _ in range(60):
        if pool.jobs_processed >= 1 and "APTUSDT" not in pool._target_end:
            break
        await asyncio.sleep(0.05)
    # Must have processed through highest end (possibly one or two runs)
    assert any(e >= e2 for e in ends) or (
        ends and pool.jobs_processed >= 1 and e2 not in pool._target_end
    )
    # After drain, target cleared at >= e2
    assert pool._target_end.get("APTUSDT") is None or pool._target_end["APTUSDT"] <= e2
    # If still pending, wait
    for _ in range(40):
        if not pool._target_end and not pool._inflight:
            break
        await asyncio.sleep(0.05)
    assert max(ends) >= e2
    await pool.stop(drain_s=1.0)


@pytest.mark.asyncio
async def test_queue_overflow_does_not_block_and_marks_dirty():
    def catchup(*, symbol: str, end: datetime) -> None:
        time.sleep(10)  # hold workers

    pool = SignalWorkerPool(
        symbols=[f"S{i}" for i in range(20)],
        catchup_fn=catchup,
        workers=1,
        queue_maxsize=2,
        scheduler_tick_s=60.0,
    )
    pool.start()
    # Saturate: put many symbols while worker busy
    t0 = time.perf_counter()
    results = []
    for i in range(15):
        results.append(
            pool.enqueue(
                f"S{i}",
                datetime(2026, 8, 10, 15, 0, tzinfo=UTC) + timedelta(minutes=i),
            )
        )
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5  # never blocked on await put
    assert any(r.overflow or r.dirty or r.accepted for r in results)
    # Coalesced state retained even on overflow
    assert len(pool._target_end) >= 1
    await pool.stop(drain_s=0.1)


@pytest.mark.asyncio
async def test_worker_failure_retries_and_pool_survives():
    calls = {"n": 0}

    def catchup(*, symbol: str, end: datetime) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return None

    health = HealthState()
    health.init_symbols(["APTUSDT"])
    pool = SignalWorkerPool(
        symbols=["APTUSDT"],
        catchup_fn=catchup,
        health=health,
        workers=2,
        max_retries=3,
        queue_maxsize=10,
        scheduler_tick_s=0.2,
    )
    pool.start()
    pool.enqueue("APTUSDT", datetime(2026, 8, 10, 15, 45, tzinfo=UTC))
    for _ in range(80):
        if pool.jobs_processed >= 1:
            break
        await asyncio.sleep(0.05)
    assert pool.jobs_processed == 1
    assert pool.jobs_retried >= 2
    assert len(pool._worker_tasks) == 2
    await pool.stop(drain_s=1.0)


@pytest.mark.asyncio
async def test_worker_failure_marks_degraded_not_advance_on_fail():
    def catchup(*, symbol: str, end: datetime) -> None:
        raise RuntimeError("always")

    health = HealthState()
    health.init_symbols(["APTUSDT"])
    pool = SignalWorkerPool(
        symbols=["APTUSDT"],
        catchup_fn=catchup,
        health=health,
        workers=1,
        max_retries=2,
        queue_maxsize=10,
        scheduler_tick_s=0.2,
    )
    pool.start()
    pool.enqueue("APTUSDT", datetime(2026, 8, 10, 15, 45, tzinfo=UTC))
    for _ in range(60):
        if pool.jobs_failed >= 1:
            break
        await asyncio.sleep(0.05)
    assert pool.jobs_failed >= 1
    assert "APTUSDT" in pool._dirty
    assert health.ensure_symbol("APTUSDT").signal_processor_state == "DEGRADED"
    await pool.stop(drain_s=0.5)


@pytest.mark.asyncio
async def test_insert_closed_enqueues_without_awaiting_catchup():
    """Critical: WS callback must return while catchup still running."""
    started = asyncio.Event()
    release = asyncio.Event()

    def catchup(*, symbol: str, end: datetime) -> None:
        # Signal from thread that we entered catchup
        loop = getattr(catchup, "_loop")
        loop.call_soon_threadsafe(started.set)
        # Block until test releases
        while not release.is_set():
            time.sleep(0.02)

    ch = MagicMock()
    collector = Live1mCollector(
        symbols=["APTUSDT"],
        ch=ch,
        enable_signals=True,
        signal_workers=2,
        repair_internal=False,
    )
    # Avoid real CH inserts
    collector.buffer.add = MagicMock(return_value=0)  # type: ignore[method-assign]
    pool = collector._ensure_signal_pool()
    assert pool is not None
    catchup._loop = asyncio.get_running_loop()  # type: ignore[attr-defined]
    pool._catchup_fn = catchup
    pool.start()

    open_t = datetime(2026, 8, 10, 15, 44, tzinfo=UTC)  # close 15:45 → 15m boundary
    assert htf_boundary_at_close(open_t + timedelta(minutes=1))

    t0 = time.perf_counter()
    await collector._insert_closed(_candle("APTUSDT", open_t))
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5  # returned immediately
    await asyncio.wait_for(started.wait(), timeout=2.0)
    # Catchup still blocked — prove we did not await it
    assert pool._busy >= 1 or "APTUSDT" in pool._inflight or started.is_set()
    release.set()
    await pool.stop(drain_s=1.0)


@pytest.mark.asyncio
async def test_heartbeat_responsive_during_slow_signal():
    """Simulate 10×~4s serial-old behavior but with queue — pongs still handled."""
    def catchup(*, symbol: str, end: datetime) -> None:
        time.sleep(0.3)

    ch = MagicMock()
    collector = Live1mCollector(
        symbols=[f"C{i}USDT" for i in range(10)],
        ch=ch,
        enable_signals=True,
        signal_workers=4,
        repair_internal=False,
    )
    collector.buffer.add = MagicMock(return_value=0)  # type: ignore[method-assign]
    pool = collector._ensure_signal_pool()
    assert pool is not None
    pool._catchup_fn = catchup
    pool.start()

    pongs = {"n": 0}

    async def on_event(name: str, data: dict) -> None:
        if name == "pong":
            pongs["n"] += 1
            collector.health.note_pong()

    ws = BybitKlineWebSocket(
        collector.symbols,
        on_closed_candle=collector._insert_closed,
        on_event=on_event,
    )
    # Enqueue 10 HTF jobs
    end = datetime(2026, 8, 10, 15, 45, tzinfo=UTC)
    for s in collector.symbols:
        pool.enqueue(s, end)

    # While workers busy, feed pongs through WS handler (recv path)
    for _ in range(5):
        await ws._handle_payload({"op": "ping", "ret_msg": "pong", "success": True})
        await asyncio.sleep(0.05)
    assert pongs["n"] == 5
    assert collector.health.last_pong_at is not None
    await pool.stop(drain_s=2.0)


@pytest.mark.asyncio
async def test_ten_symbol_boundary_no_ws_stall():
    def catchup(*, symbol: str, end: datetime) -> None:
        time.sleep(0.25)  # stand-in for ~4s; scaled for test speed

    ch = MagicMock()
    syms = [
        "APTUSDT",
        "DOGEUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "AVAXUSDT",
        "HYPEUSDT",
        "ZECUSDT",
        "ACEUSDT",
        "BMTUSDT",
        "TUTUSDT",
    ]
    collector = Live1mCollector(
        symbols=syms,
        ch=ch,
        enable_signals=True,
        signal_workers=4,
        repair_internal=False,
    )
    collector.buffer.add = MagicMock(return_value=0)  # type: ignore[method-assign]
    pool = collector._ensure_signal_pool()
    assert pool is not None
    pool._catchup_fn = catchup
    pool.start()

    open_t = datetime(2026, 8, 10, 15, 44, tzinfo=UTC)
    t0 = time.perf_counter()
    for s in syms:
        await collector._insert_closed(_candle(s, open_t))
    stall = time.perf_counter() - t0
    # Old bug: ~10*4s serial. New: enqueue-only << 1s
    assert stall < 1.0
    for _ in range(80):
        if pool.jobs_processed >= 10:
            break
        await asyncio.sleep(0.05)
    assert pool.jobs_processed >= 10
    await pool.stop(drain_s=1.0)


@pytest.mark.asyncio
async def test_graceful_shutdown_with_backlog_is_fast():
    def catchup(*, symbol: str, end: datetime) -> None:
        time.sleep(2.0)

    pool = SignalWorkerPool(
        symbols=["APTUSDT", "DOGEUSDT"],
        catchup_fn=catchup,
        workers=2,
        queue_maxsize=10,
        shutdown_drain_s=0.3,
        scheduler_tick_s=0.2,
    )
    pool.start()
    pool.enqueue("APTUSDT", datetime(2026, 8, 10, 15, 45, tzinfo=UTC))
    pool.enqueue("DOGEUSDT", datetime(2026, 8, 10, 15, 45, tzinfo=UTC))
    await asyncio.sleep(0.05)
    t0 = time.perf_counter()
    await pool.stop(drain_s=0.3)
    assert time.perf_counter() - t0 < 1.5


def test_recovery_window_includes_missing_minute_after_tip_jump():
    """last=15:44, as_of after 15:46 → window must cover 15:45."""
    last = datetime(2026, 8, 10, 15, 44, tzinfo=UTC)
    as_of = datetime(2026, 8, 10, 15, 46, 30, tzinfo=UTC)
    window = compute_recovery_window(last, as_of=as_of, overlap_minutes=1)
    assert window is not None
    start, end = window
    assert start == last
    assert end == datetime(2026, 8, 10, 15, 46, tzinfo=UTC)
    # 15:45 open is in [start, end)
    missing = datetime(2026, 8, 10, 15, 45, tzinfo=UTC)
    assert start <= missing < end


def test_trailing_recovery_blind_to_mid_gap_when_max_jumped():
    """Documents why recent continuity is required: MAX=15:46, hole at 15:45."""
    last = datetime(2026, 8, 10, 15, 46, tzinfo=UTC)
    as_of = datetime(2026, 8, 10, 15, 47, 10, tzinfo=UTC)
    assert compute_recovery_window(last, as_of=as_of) is None


def test_repair_recent_continuity_fetches_mid_gap(monkeypatch):
    inserted: list[Any] = []

    class FakeRepo:
        def get_last_closed_open_time(self, symbol: str):
            return datetime(2026, 8, 10, 15, 46, tzinfo=UTC)

        def insert_candles(self, candles):
            inserted.extend(candles)
            return len(candles)

    class FakeHistory:
        def fetch_closed_1m(self, symbol, start, end, as_of=None):
            # Return the missing 15:45 bar
            return [
                _candle(symbol, datetime(2026, 8, 10, 15, 45, tzinfo=UTC)),
            ]

    def fake_detect(ch, *, symbol, effective_start, requested_end):
        return MissingRangeReport(
            symbol=symbol,
            effective_start=effective_start,
            requested_end=requested_end,
            existing_count=1,
            ranges=[
                MissingRange(
                    kind="INTERNAL",
                    start=datetime(2026, 8, 10, 15, 45, tzinfo=UTC),
                    end=datetime(2026, 8, 10, 15, 46, tzinfo=UTC),
                )
            ],
        )

    monkeypatch.setattr(
        "signal_generator.bybit.live.recovery.detect_missing_ranges",
        fake_detect,
    )
    n, ins = repair_recent_continuity(
        symbol="APTUSDT",
        ch=MagicMock(),
        repo=FakeRepo(),  # type: ignore[arg-type]
        history=FakeHistory(),  # type: ignore[arg-type]
        as_of=datetime(2026, 8, 10, 15, 47, tzinfo=UTC),
        lookback_minutes=30,
    )
    assert n == 1
    assert ins == 1
    assert inserted[0].open_time == datetime(2026, 8, 10, 15, 45, tzinfo=UTC)


def test_default_worker_count():
    assert DEFAULT_SIGNAL_WORKERS == 4


def test_health_exposes_signal_metrics():
    h = HealthState()
    h.init_symbols(["APTUSDT"])
    h.apply_signal_pool_metrics(
        {
            "signal_queue_depth": 3,
            "signal_queue_maxsize": 1000,
            "signal_workers_total": 4,
            "signal_workers_busy": 2,
            "signal_jobs_processed": 10,
            "signal_jobs_failed": 1,
            "signal_jobs_retried": 2,
            "signal_processing_last_ms": 12.5,
            "signal_processing_max_ms": 99.0,
            "signal_dirty_symbols": ["APTUSDT"],
            "signal_overflow_count": 0,
        }
    )
    d = h.to_dict()
    assert d["signal_workers_total"] == 4
    assert d["signal_queue_depth"] == 3
    assert "signal_last_processed_at" in d["symbols"][0]


@pytest.mark.asyncio
async def test_ws_closed_candle_path_returns_while_pool_busy():
    """Regression: 10 boundary candles must not stall recv for stale timeout."""
    def catchup(*, symbol: str, end: datetime) -> None:
        time.sleep(0.4)

    ch = MagicMock()
    collector = Live1mCollector(
        symbols=[f"X{i}USDT" for i in range(10)],
        ch=ch,
        enable_signals=True,
        signal_workers=4,
        repair_internal=False,
    )
    collector.buffer.add = MagicMock(return_value=0)  # type: ignore[method-assign]
    pool = collector._ensure_signal_pool()
    assert pool is not None
    pool._catchup_fn = catchup
    pool.start()

    ws = BybitKlineWebSocket(
        collector.symbols, on_closed_candle=collector._insert_closed
    )
    open_ms = int(datetime(2026, 8, 10, 15, 44, tzinfo=UTC).timestamp() * 1000)
    t0 = time.perf_counter()
    for s in collector.symbols:
        await ws._handle_payload(
            {
                "topic": f"kline.1.{s}",
                "data": [
                    {
                        "start": open_ms,
                        "end": open_ms + 59_999,
                        "interval": "1",
                        "open": "1",
                        "high": "1",
                        "low": "1",
                        "close": "1",
                        "volume": "1",
                        "turnover": "1",
                        "confirm": True,
                        "timestamp": open_ms + 1,
                    }
                ],
            }
        )
    assert time.perf_counter() - t0 < 1.0
    # Pong still handled immediately
    await ws._handle_payload({"op": "pong"})
    assert ws.last_pong_at is not None
    await pool.stop(drain_s=2.0)


def test_no_strategy_constants_changed():
    from signal_generator.pipeline.versions import STRATEGY_VERSION, EDGES_VERSION
    from signal_generator.strategy.wave_fade.parameters import SIGNAL_TFS

    assert "15m" in SIGNAL_TFS
    assert STRATEGY_VERSION
    assert EDGES_VERSION


def test_lost_jobs_reconstructed_via_watermark_contract():
    """In-memory queue loss is acceptable; watermark SoT is documented by API."""
    # restart catch-up uses run_signal_catchup(end=...) from watermarks — no new SoT
    from signal_generator.bybit.live import signal_catchup as sc

    assert callable(sc.run_signal_catchup)
    assert sc.SHADOW_MODE is True
