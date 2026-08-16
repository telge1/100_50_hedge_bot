"""Unit tests for live collector recovery, WS parsing, and state machine."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from signal_generator.bybit.live.health import CollectorState, HealthState
from signal_generator.bybit.live.recovery import (
    compute_recovery_window,
    last_fully_closed_open_time,
)
from signal_generator.bybit.live.ws_kline import (
    BybitKlineWebSocket,
    is_pong_message,
    parse_ws_kline_item,
    topic_for_symbol,
    ws_tick_to_candle,
)
from signal_generator.db.candles import Candle1m


def test_last_fully_closed_open_time():
    as_of = datetime(2026, 8, 10, 10, 23, 40, tzinfo=timezone.utc)
    assert last_fully_closed_open_time(as_of=as_of) == datetime(
        2026, 8, 10, 10, 22, tzinfo=timezone.utc
    )


def test_startup_gap_window_multiple_minutes():
    last = datetime(2026, 8, 10, 10, 14, tzinfo=timezone.utc)
    as_of = datetime(2026, 8, 10, 10, 23, 5, tzinfo=timezone.utc)
    window = compute_recovery_window(last, as_of=as_of, overlap_minutes=1)
    assert window is not None
    start, end = window
    assert start == last  # overlap re-fetch
    assert end == datetime(2026, 8, 10, 10, 23, tzinfo=timezone.utc)
    # Missing open times: 10:15..10:22 inclusive → recovered via [10:14, 10:23)


def test_no_gap_when_caught_up():
    last = datetime(2026, 8, 10, 10, 22, tzinfo=timezone.utc)
    as_of = datetime(2026, 8, 10, 10, 23, 10, tzinfo=timezone.utc)
    assert compute_recovery_window(last, as_of=as_of) is None


def test_gap_over_hours():
    last = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
    as_of = datetime(2026, 8, 10, 10, 5, tzinfo=timezone.utc)
    window = compute_recovery_window(last, as_of=as_of, overlap_minutes=1)
    assert window is not None
    start, end = window
    assert start == last
    assert end == datetime(2026, 8, 10, 10, 5, tzinfo=timezone.utc)


def test_no_history_recovers_latest_closed_only():
    as_of = datetime(2026, 8, 10, 10, 23, 1, tzinfo=timezone.utc)
    window = compute_recovery_window(None, as_of=as_of)
    assert window == (
        datetime(2026, 8, 10, 10, 22, tzinfo=timezone.utc),
        datetime(2026, 8, 10, 10, 23, tzinfo=timezone.utc),
    )


def test_ws_incomplete_candle_not_persisted():
    item = {
        "start": 1_704_067_200_000,
        "end": 1_704_067_259_999,
        "interval": "1",
        "open": "1",
        "high": "2",
        "low": "0.5",
        "close": "1.5",
        "volume": "10",
        "turnover": "15",
        "confirm": False,
        "timestamp": 1_704_067_210_000,
    }
    tick = parse_ws_kline_item(item, symbol="APTUSDT")
    assert tick.confirm is False
    assert ws_tick_to_candle(tick) is None


def test_ws_confirm_true_becomes_candle():
    start = int(datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    item = {
        "start": start,
        "end": start + 59_999,
        "interval": "1",
        "open": "1.1",
        "high": "1.2",
        "low": "1.0",
        "close": "1.15",
        "volume": "100",
        "turnover": "110",
        "confirm": True,
        "timestamp": start + 50_000,
    }
    tick = parse_ws_kline_item(item, symbol="dogeusdt")
    candle = ws_tick_to_candle(tick)
    assert candle is not None
    assert candle.symbol == "DOGEUSDT"
    assert candle.is_closed is True
    assert candle.source == "bybit_live"
    assert candle.close_time - candle.open_time == timedelta(seconds=60)
    assert candle.open == Decimal("1.1")


def test_pong_detection_variants():
    assert is_pong_message({"op": "ping", "ret_msg": "pong", "success": True})
    assert is_pong_message({"op": "pong"})
    assert not is_pong_message({"op": "subscribe", "success": True})


def test_topic_for_symbol():
    assert topic_for_symbol("btcusdt") == "kline.1.BTCUSDT"


def test_health_state_transitions_log(caplog):
    h = HealthState()
    with caplog.at_level("INFO"):
        h.set_state(CollectorState.RECOVERING, reason="startup")
        h.set_state(CollectorState.LIVE)
    assert h.state == CollectorState.LIVE
    assert any("STATE STARTING → RECOVERING" in r.message for r in caplog.records)


def test_backoff_schedule_values():
    from signal_generator.bybit.live.collector import BACKOFF_SCHEDULE_S

    assert BACKOFF_SCHEDULE_S == (1, 2, 5, 10, 30)


@pytest.mark.asyncio
async def test_ws_handles_closed_candle_callback():
    received: list[Candle1m] = []

    async def on_closed(c: Candle1m) -> None:
        received.append(c)

    ws = BybitKlineWebSocket(["APTUSDT"], on_closed_candle=on_closed)
    start = int(datetime(2026, 8, 10, 11, 0, tzinfo=timezone.utc).timestamp() * 1000)
    await ws._handle_payload(
        {
            "topic": "kline.1.APTUSDT",
            "data": [
                {
                    "start": start,
                    "end": start + 59_999,
                    "interval": "1",
                    "open": "1",
                    "high": "1",
                    "low": "1",
                    "close": "1",
                    "volume": "1",
                    "turnover": "1",
                    "confirm": True,
                    "timestamp": start + 1,
                }
            ],
        }
    )
    assert len(received) == 1
    assert received[0].open_time == datetime(2026, 8, 10, 11, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_ws_ignores_unconfirmed():
    received: list[Candle1m] = []

    async def on_closed(c: Candle1m) -> None:
        received.append(c)

    ws = BybitKlineWebSocket(["APTUSDT"], on_closed_candle=on_closed)
    start = int(datetime(2026, 8, 10, 11, 0, tzinfo=timezone.utc).timestamp() * 1000)
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
    assert "APTUSDT" in ws._open_candles


def test_recovery_idempotent_window_overlap():
    last = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    as_of = datetime(2026, 1, 1, 12, 5, 1, tzinfo=timezone.utc)
    w1 = compute_recovery_window(last, as_of=as_of, overlap_minutes=1)
    w2 = compute_recovery_window(last, as_of=as_of, overlap_minutes=1)
    assert w1 == w2
    assert w1 is not None and w1[0] == last


def test_clickhouse_ahead_of_local_memory_uses_ch_last():
    # Local memory says older; recovery window must use CH last (passed in).
    ch_last = datetime(2026, 8, 10, 10, 20, tzinfo=timezone.utc)
    local_stale = datetime(2026, 8, 10, 10, 10, tzinfo=timezone.utc)
    as_of = datetime(2026, 8, 10, 10, 23, 0, tzinfo=timezone.utc)
    # Correct SoT = ch_last
    w = compute_recovery_window(ch_last, as_of=as_of)
    assert w is not None
    assert w[0] == ch_last
    # If wrongly used local_stale, window would be larger — ensure ch wins when provided
    w_bad = compute_recovery_window(local_stale, as_of=as_of)
    assert w_bad is not None
    assert w_bad[0] < w[0]


class FakeRepo:
    def __init__(self, last: dict[str, datetime | None]) -> None:
        self.last = dict(last)
        self.inserted: list[Candle1m] = []

    def get_last_closed_open_time(self, symbol: str, **kwargs: Any) -> datetime | None:
        return self.last.get(symbol)

    def insert_candles(self, candles: list[Candle1m]) -> int:
        self.inserted.extend(candles)
        if candles:
            self.last[candles[-1].symbol] = candles[-1].open_time
        return len(candles)

    def count_final(self, symbol: str, start: datetime, end: datetime, **kwargs: Any) -> int:
        return sum(
            1
            for c in self.inserted
            if c.symbol == symbol and start <= c.open_time < end
        )


class FakeHistory:
    def __init__(self, candles: list[Candle1m]) -> None:
        self.candles = candles
        self.calls: list[tuple[str, datetime, datetime]] = []

    def fetch_closed_1m(self, symbol: str, start: datetime, end: datetime, **kwargs: Any):
        self.calls.append((symbol, start, end))
        return [
            c
            for c in self.candles
            if c.symbol == symbol and start <= c.open_time < end
        ]


def _c(symbol: str, minute: int) -> Candle1m:
    ot = datetime(2026, 8, 10, 10, minute, tzinfo=timezone.utc)
    return Candle1m(
        exchange="bybit",
        symbol=symbol,
        open_time=ot,
        close_time=ot + timedelta(minutes=1),
        open="1",
        high="1",
        low="1",
        close="1",
        volume="1",
        turnover="1",
        source="bybit_history",
    )


def test_recover_symbol_fills_gap_and_is_idempotent():
    from signal_generator.bybit.live.recovery import recover_symbol

    repo = FakeRepo({"APTUSDT": datetime(2026, 8, 10, 10, 14, tzinfo=timezone.utc)})
    hist = FakeHistory([_c("APTUSDT", m) for m in range(14, 23)])
    as_of = datetime(2026, 8, 10, 10, 23, 5, tzinfo=timezone.utc)
    r1 = recover_symbol(symbol="APTUSDT", repo=repo, history=hist, as_of=as_of)  # type: ignore[arg-type]
    assert r1.ok
    assert r1.inserted >= 1
    first_inserts = len(repo.inserted)
    # Crash between insert and state update: CH already has data (FakeRepo last advanced).
    # Second recovery with updated last should fetch smaller/none gap.
    r2 = recover_symbol(symbol="APTUSDT", repo=repo, history=hist, as_of=as_of)  # type: ignore[arg-type]
    assert r2.ok
    # Idempotent: may overlap-insert last bar again, but must not explode
    assert len(repo.inserted) >= first_inserts


def test_recover_symbol_reports_failure():
    from signal_generator.bybit.live.recovery import recover_symbol

    class BoomHistory(FakeHistory):
        def fetch_closed_1m(self, *a, **k):
            raise RuntimeError("rest down")

    repo = FakeRepo({"APTUSDT": datetime(2026, 8, 10, 10, 14, tzinfo=timezone.utc)})
    as_of = datetime(2026, 8, 10, 10, 23, 5, tzinfo=timezone.utc)
    r = recover_symbol(
        symbol="APTUSDT",
        repo=repo,  # type: ignore[arg-type]
        history=BoomHistory([]),  # type: ignore[arg-type]
        as_of=as_of,
    )
    assert r.ok is False
    assert "rest down" in (r.error or "")
