"""Audit: 1m closed candle is the only live signal clock (HTF boundary gate)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from signal_generator.bybit.live.collector import Live1mCollector
from signal_generator.bybit.live.signal_catchup import (
    htf_boundaries_at_close,
    htf_boundary_at_close,
    signal_catchup_end_exclusive,
)
from signal_generator.bybit.live.ws_kline import BybitKlineWebSocket, ws_tick_to_candle
from signal_generator.db.candles import Candle1m
from signal_generator.pipeline.versions import STRATEGY_VERSION
from signal_generator.strategy.wave_fade.parameters import SIGNAL_TFS
from signal_generator.timeframes import (
    STRATEGY_TIMEFRAMES,
    aggregate_1m_to_timeframe,
    bucket_close,
    bucket_start,
    ensure_utc,
)


UTC = timezone.utc


def _ct(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 10, hour, minute, tzinfo=UTC)


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


# ---------------------------------------------------------------------------
# Boundary matrix (UTC epoch-aligned buckets)
# ---------------------------------------------------------------------------

BOUNDARY_MATRIX = [
    # (close_time H:M, expected strategy TFs)
    ((16, 29), ()),
    ((16, 30), ("15m", "30m")),
    ((16, 44), ()),
    ((16, 45), ("15m",)),
    ((16, 59), ()),
    ((17, 0), ("15m", "30m", "1h")),
    ((19, 59), ()),
    ((20, 0), ("15m", "30m", "1h", "4h")),
]


@pytest.mark.parametrize("hm,expected", BOUNDARY_MATRIX)
def test_boundary_matrix(hm, expected):
    close = _ct(*hm)
    actual = htf_boundaries_at_close(close)
    assert actual == expected
    assert htf_boundary_at_close(close) is bool(expected)


def test_catchup_end_exclusive_includes_boundary_available_at():
    assert signal_catchup_end_exclusive(_ct(16, 30)) == _ct(16, 31)
    assert signal_catchup_end_exclusive(_ct(17, 0)) == _ct(17, 1)


def test_signal_tfs_exclude_5m():
    assert STRATEGY_TIMEFRAMES == ("15m", "30m", "1h", "4h")
    assert SIGNAL_TFS == ("15m", "30m", "1h", "4h")
    assert "5m" not in STRATEGY_TIMEFRAMES
    assert "5m" not in SIGNAL_TFS
    # :35 would be a 5m close — must NOT trigger strategy jobs
    assert htf_boundaries_at_close(_ct(16, 35)) == ()
    assert htf_boundary_at_close(_ct(16, 35)) is False


def test_non_boundary_minutes_no_trigger():
    for m in (31, 32, 33, 34, 36, 37, 38, 39, 41):
        assert htf_boundaries_at_close(_ct(16, m)) == ()


def test_available_at_equals_htf_close_time():
    open_t = datetime(2026, 8, 10, 16, 15, tzinfo=UTC)
    close_t = bucket_close(open_t, "15m")
    assert close_t == _ct(16, 30)
    from signal_generator.timeframes import OhlcvBar

    bar = OhlcvBar(
        open_time=open_t,
        close_time=close_t,
        open=1,
        high=1,
        low=1,
        close=1,
        volume=1,
        turnover=1,
    )
    assert bar.available_at == close_t


def test_no_lookahead_15m_bucket():
    """15m 16:15–16:30 may only aggregate with as_of >= 16:30; no 16:30 1m bar."""
    from signal_generator.timeframes import OhlcvBar

    start = datetime(2026, 8, 10, 16, 15, tzinfo=UTC)
    closed_1m = []
    for i in range(15):
        ot = start + timedelta(minutes=i)
        closed_1m.append(
            OhlcvBar(
                open_time=ot,
                close_time=ot + timedelta(minutes=1),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0 + i * 0.01,
                volume=1.0,
                turnover=1.0,
            )
        )
    # Premature as_of → no closed 15m bar
    assert (
        aggregate_1m_to_timeframe(
            closed_1m,
            "15m",
            as_of=datetime(2026, 8, 10, 16, 29, 59, tzinfo=UTC),
        )
        == []
    )
    out = aggregate_1m_to_timeframe(closed_1m, "15m", as_of=_ct(16, 30))
    assert len(out) == 1
    assert out[0].close_time == _ct(16, 30)
    assert out[0].available_at == _ct(16, 30)
    # Future 1m open 16:30 must not change the completed 15m bar
    future = OhlcvBar(
        open_time=_ct(16, 30),
        close_time=_ct(16, 31),
        open=99.0,
        high=99.0,
        low=99.0,
        close=99.0,
        volume=1.0,
        turnover=1.0,
    )
    out2 = aggregate_1m_to_timeframe(closed_1m + [future], "15m", as_of=_ct(16, 30))
    assert len(out2) == 1
    assert out2[0].close == out[0].close


@pytest.mark.asyncio
async def test_non_boundary_persist_no_enqueue():
    ch = MagicMock()
    collector = Live1mCollector(
        symbols=["APTUSDT"],
        ch=ch,
        enable_signals=True,
        repair_internal=False,
        signal_workers=2,
    )
    collector.buffer.add = MagicMock(return_value=0)  # type: ignore[method-assign]
    pool = collector._ensure_signal_pool()
    assert pool is not None
    enq = MagicMock(wraps=pool.enqueue)
    pool.enqueue = enq  # type: ignore[method-assign]
    pool.start()
    # close_time = 16:31 → non-boundary
    await collector._insert_closed(_candle("APTUSDT", _ct(16, 30)))  # open 16:30 close 16:31
    assert enq.call_count == 0
    await pool.stop(drain_s=0.2)


@pytest.mark.asyncio
async def test_15m_boundary_enqueues_one_job():
    ch = MagicMock()
    collector = Live1mCollector(
        symbols=["APTUSDT"],
        ch=ch,
        enable_signals=True,
        repair_internal=False,
        signal_workers=2,
    )
    collector.buffer.add = MagicMock(return_value=0)  # type: ignore[method-assign]
    pool = collector._ensure_signal_pool()
    assert pool is not None
    calls: list[tuple] = []

    def track(symbol, end, *, reason=""):
        calls.append((symbol, end, reason))
        return pool.__class__.enqueue(pool, symbol, end, reason=reason)

    pool.enqueue = track  # type: ignore[method-assign]
    pool.start()
    # open 16:44 → close 16:45 → 15m only
    await collector._insert_closed(_candle("APTUSDT", _ct(16, 44)))
    assert len(calls) == 1
    assert calls[0][0] == "APTUSDT"
    assert calls[0][1] == _ct(16, 46)  # exclusive end past 16:45 available_at
    assert htf_boundaries_at_close(_ct(16, 45)) == ("15m",)
    await pool.stop(drain_s=0.2)


@pytest.mark.asyncio
async def test_multi_tf_boundary_single_job_covers_all_tfs():
    """One enqueue(symbol, end=17:00); catch-up pipeline covers all STRATEGY_TFS."""
    close = _ct(17, 0)
    assert htf_boundaries_at_close(close) == ("15m", "30m", "1h")
    ch = MagicMock()
    collector = Live1mCollector(
        symbols=["APTUSDT"],
        ch=ch,
        enable_signals=True,
        repair_internal=False,
    )
    collector.buffer.add = MagicMock(return_value=0)  # type: ignore[method-assign]
    pool = collector._ensure_signal_pool()
    assert pool is not None
    ends: list[datetime] = []

    def track(symbol, end, *, reason=""):
        ends.append(end)
        return type(pool).enqueue(pool, symbol, end, reason=reason)

    pool.enqueue = track  # type: ignore[method-assign]
    pool.start()
    await collector._insert_closed(_candle("APTUSDT", _ct(16, 59)))  # close 17:00
    assert ends == [_ct(17, 1)]  # exclusive end
    await pool.stop(drain_s=0.2)


@pytest.mark.asyncio
async def test_4h_boundary_20_00():
    assert htf_boundaries_at_close(_ct(20, 0)) == ("15m", "30m", "1h", "4h")
    ch = MagicMock()
    collector = Live1mCollector(
        symbols=["APTUSDT"], ch=ch, enable_signals=True, repair_internal=False
    )
    collector.buffer.add = MagicMock(return_value=0)  # type: ignore[method-assign]
    pool = collector._ensure_signal_pool()
    assert pool is not None
    ends: list[datetime] = []
    pool.enqueue = lambda s, e, *, reason="": ends.append(e) or type(pool).enqueue(  # type: ignore[method-assign]
        pool, s, e, reason=reason
    )
    pool.start()
    await collector._insert_closed(_candle("APTUSDT", _ct(19, 59)))
    assert ends == [_ct(20, 1)]
    await pool.stop(drain_s=0.2)


@pytest.mark.asyncio
async def test_confirm_false_ignored_no_job():
    received: list = []

    async def on_closed(c):
        received.append(c)

    ws = BybitKlineWebSocket(["APTUSDT"], on_closed_candle=on_closed)
    start = int(_ct(16, 44).timestamp() * 1000)
    await ws._handle_payload(
        {
            "topic": "kline.1.APTUSDT",
            "data": [
                {
                    "start": start,
                    "end": start + 10_000,
                    "interval": "1",
                    "open": "1",
                    "high": "1",
                    "low": "1",
                    "close": "1",
                    "volume": "1",
                    "turnover": "1",
                    "confirm": False,
                    "timestamp": start + 1,
                }
            ],
        }
    )
    assert received == []
    assert ws_tick_to_candle(
        __import__(
            "signal_generator.bybit.live.ws_kline", fromlist=["parse_ws_kline_item"]
        ).parse_ws_kline_item(
            {
                "start": start,
                "end": start + 10_000,
                "interval": "1",
                "open": "1",
                "high": "1",
                "low": "1",
                "close": "1",
                "volume": "1",
                "turnover": "1",
                "confirm": False,
            },
            symbol="APTUSDT",
        )
    ) is None


@pytest.mark.asyncio
async def test_ws_path_non_blocking_on_boundary():
    def slow(*, symbol, end):
        time.sleep(0.4)

    ch = MagicMock()
    collector = Live1mCollector(
        symbols=["APTUSDT"],
        ch=ch,
        enable_signals=True,
        repair_internal=False,
        signal_workers=1,
    )
    collector.buffer.add = MagicMock(return_value=0)  # type: ignore[method-assign]
    pool = collector._ensure_signal_pool()
    assert pool is not None
    pool._catchup_fn = slow
    pool.start()
    t0 = time.perf_counter()
    await collector._insert_closed(_candle("APTUSDT", _ct(16, 44)))
    assert time.perf_counter() - t0 < 0.25
    await pool.stop(drain_s=1.0)


def test_restart_recovers_unprocessed_boundary_via_watermark():
    """Candle at boundary exists; watermark behind → catch-up must process bucket."""
    from signal_generator.db.processing_state import ProcessingState
    from signal_generator.pipeline.processor import (
        ShadowPipelineConfig,
        WaveFadeShadowPipeline,
    )
    from signal_generator.pipeline.versions import MODE_SHADOW
    from signal_generator.timeframes import OhlcvBar, bars_from_mappings

    # Minimal fake repos
    class FakeState:
        def __init__(self):
            # last processed 15m open = 16:00 → next is 16:15 closing 16:30
            self.st = ProcessingState(
                symbol="APTUSDT",
                timeframe="15m",
                strategy_version=STRATEGY_VERSION,
                last_processed_candle_open_time=datetime(
                    2026, 8, 10, 16, 0, tzinfo=UTC
                ),
                last_processed_available_at=datetime(2026, 8, 10, 16, 15, tzinfo=UTC),
                exchange="bybit",
            )
            self.upserts = []

        def get(self, symbol, timeframe, strategy_version, exchange="bybit"):
            if timeframe == "15m":
                return self.st
            return None

        def upsert(self, state):
            self.upserts.append(state)

    class FakeCandles:
        def get_candles(self, symbol, start, end, *, exchange="bybit", interval="1m"):
            # Provide enough 1m for lookback-ish: just 16:00..16:29
            rows = []
            t = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
            while t < datetime(2026, 8, 10, 16, 30, tzinfo=UTC):
                rows.append(
                    {
                        "exchange": "bybit",
                        "symbol": symbol,
                        "interval": "1m",
                        "open_time": t,
                        "close_time": t + timedelta(minutes=1),
                        "open": 1.0,
                        "high": 1.0,
                        "low": 1.0,
                        "close": 1.0,
                        "volume": 1.0,
                        "turnover": 1.0,
                        "is_closed": 1,
                        "source": "test",
                        "source_event_time": None,
                        "ingested_at": t,
                    }
                )
                t += timedelta(minutes=1)
            return [r for r in rows if start <= r["open_time"] < end]

    class FakeSignals:
        def insert_signals(self, batch):
            return len(batch)

    cfg = ShadowPipelineConfig(
        symbols=["APTUSDT"],
        start=datetime(2026, 8, 10, 16, 0, tzinfo=UTC),
        end=datetime(2026, 8, 10, 16, 31, tzinfo=UTC),  # exclusive past 16:30 boundary
        mode=MODE_SHADOW,
        shadow=True,
        lookback=timedelta(hours=2),
        timeframes=("15m",),
    )
    state = FakeState()
    pipe = WaveFadeShadowPipeline(
        candles=FakeCandles(),  # type: ignore[arg-type]
        signals=FakeSignals(),  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        config=cfg,
        edges={},  # empty → no signal rows but watermark still advances
    )
    # Empty edges may cause build_symbol_signals issues — use load or skip signals
    from signal_generator.strategy.wave_fade.edges import load_frozen_eff_edges

    pipe.edges = load_frozen_eff_edges()
    metrics = pipe.run()
    assert metrics.errors == 0
    assert any(
        u.last_processed_available_at == datetime(2026, 8, 10, 16, 30, tzinfo=UTC)
        for u in state.upserts
        if u.timeframe == "15m"
    )


def test_no_5m_scheduler_in_live_path():
    import inspect
    from signal_generator.bybit.live import collector as col
    from signal_generator.bybit.live import signal_catchup as sc

    src = inspect.getsource(col) + inspect.getsource(sc)
    assert "htf_boundary_at_close" in src
    # No dedicated 5m timer / interval scheduler for signals
    assert "asyncio.sleep(300" not in src
    assert "timedelta(minutes=5)" not in inspect.getsource(sc.htf_boundaries_at_close)


def test_4h_buckets_utc_epoch_aligned():
    """Document: 4h opens at 00/04/08/12/16/20 UTC."""
    for hour in (0, 4, 8, 12, 16, 20):
        close = _ct(hour, 0) if hour else datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
        open_c = close - timedelta(hours=4)
        assert bucket_start(open_c, "4h") == open_c
        assert bucket_close(open_c, "4h") == close
        assert "4h" in htf_boundaries_at_close(close)
